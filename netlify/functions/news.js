/**
 * UPSC Samachar — News Serverless Function
 * Handles ONLY RSS feeds: The Hindu, Indian Express, Economic Times
 * PIB is handled separately via static JSON built by scrape_pib.py
 */
const fetch = require("node-fetch");
const xml2js = require("xml2js");

const RSS_SOURCES = [
  {
    id: "hindu",
    name: "The Hindu",
    fullName: "The Hindu",
    color: "#DC2626",
    feeds: [
      "https://www.thehindu.com/news/national/feeder/default.rss",
      "https://www.thehindu.com/opinion/feeder/default.rss",
      "https://www.thehindu.com/sci-tech/feeder/default.rss",
      "https://www.thehindu.com/business/Economy/feeder/default.rss",
      "https://www.thehindu.com/news/international/feeder/default.rss",
    ],
  },
  {
    id: "indianexpress",
    name: "Indian Express",
    fullName: "The Indian Express",
    color: "#2563EB",
    feeds: [
      "https://indianexpress.com/section/india/feed/",
      "https://indianexpress.com/section/opinion/feed/",
      "https://indianexpress.com/section/explained/feed/",
      "https://indianexpress.com/section/economy/feed/",
      "https://indianexpress.com/section/world/feed/",
    ],
  },
  {
    id: "et",
    name: "Economic Times",
    fullName: "The Economic Times",
    color: "#059669",
    feeds: [
      "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms",
      "https://economictimes.indiatimes.com/news/politics-and-nation/rssfeeds/1052732854.cms",
      "https://economictimes.indiatimes.com/environment/rssfeeds/22977979.cms",
      "https://economictimes.indiatimes.com/news/india/rssfeeds/13357270.cms",
    ],
  },
];

const UPSC_TOPICS = {
  "Polity & Governance": ["parliament","constitution","supreme court","high court","election","amendment","bill","act","ministry","government","cabinet","president","governor","lok sabha","rajya sabha","judiciary","panchayat","governance","reform","policy","commission","ordinance"],
  "Economy": ["gdp","inflation","rbi","sebi","budget","fiscal","monetary","repo rate","economy","trade","export","import","fdi","startup","msme","agriculture","msp","niti aayog","economic","tax","gst","growth","market","rupee","investment","revenue","finance","bank"],
  "Environment & Ecology": ["climate","biodiversity","forest","wildlife","pollution","carbon","emission","renewable","solar","ozone","ramsar","tiger","elephant","coral","wetland","deforestation","net zero","cop","ipcc","ecology","conservation","environment","water","river","drought"],
  "Science & Technology": ["isro","space","nasa","satellite","ai","artificial intelligence","quantum","nuclear","research","technology","5g","semiconductor","drone","cyber","digital","blockchain","genomics","innovation","patent","rocket","launch"],
  "International Relations": ["bilateral","treaty","summit","united nations","world bank","imf","wto","g20","brics","sco","asean","nato","geopolitics","diplomacy","foreign","sanctions","agreement","alliance","visit","mou","quad"],
  "Social Issues": ["poverty","welfare","scheme","education","health","nutrition","women","child","tribal","dalit","minority","reservation","caste","disability","elderly","hunger","literacy","inequality","yojana","programme"],
  "Defence & Security": ["defence","military","army","navy","air force","border","security","terrorism","naxal","insurgency","weapon","missile","drdo","iaf","coast guard","exercise","combat","strategic"],
  "Infrastructure & Development": ["railway","highway","port","airport","metro","smart city","urban","housing","construction","energy","power","grid","infrastructure","expressway","corridor","project","bridge","dam"],
};

const ALL_KEYWORDS = Object.values(UPSC_TOPICS).flat();

function detectTopics(title, description) {
  const text = (title + " " + description).toLowerCase();
  const matched = [];
  for (const [topic, keywords] of Object.entries(UPSC_TOPICS)) {
    if (keywords.some(k => text.includes(k))) matched.push(topic);
  }
  return matched.length ? matched.slice(0, 3) : ["General"];
}

function isRelevant(title, description) {
  const text = (title + " " + description).toLowerCase();
  return ALL_KEYWORDS.some(k => text.includes(k)) || text.includes("india") || text.includes("government");
}

async function parseFeed(url, source) {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 9000);
    const response = await fetch(url, {
      signal: ctrl.signal,
      headers: {
        "User-Agent": "Mozilla/5.0 (compatible; UPSCSamachar/1.0)",
        Accept: "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
      },
    });
    clearTimeout(t);
    if (!response.ok) return [];

    const text = await response.text();
    const parser = new xml2js.Parser({ explicitArray: false, ignoreAttrs: false, trim: true });
    const result = await parser.parseStringPromise(text);

    let items = [];
    if (result?.rss?.channel?.item) {
      items = Array.isArray(result.rss.channel.item) ? result.rss.channel.item : [result.rss.channel.item];
    } else if (result?.feed?.entry) {
      items = Array.isArray(result.feed.entry) ? result.feed.entry : [result.feed.entry];
    }

    return items.slice(0, 20).map((item) => {
      const title = (item.title?._ || item.title || "").replace(/<[^>]*>/g, "").trim();
      const rawDesc = item.description?._ || item.description || item.summary?._ || item.summary || item["content:encoded"] || "";
      const description = rawDesc.replace(/<[^>]*>/g, "").replace(/\s+/g, " ").trim().slice(0, 500);
      const link = item.link?.$?.href || item.link?._ || item.link || "";
      const pubDateRaw = item.pubDate || item.updated || item["dc:date"] || new Date().toISOString();
      const category = (item.category?._ || item.category || "").replace(/<[^>]*>/g, "").trim();
      const imageUrl = item["media:content"]?.$?.url || item["media:thumbnail"]?.$?.url || item.enclosure?.$?.url || "";

      if (!title || !isRelevant(title, description)) return null;

      return {
        id: Buffer.from((link || title).slice(0, 80)).toString("base64").replace(/[^a-zA-Z0-9]/g, "").slice(0, 24),
        title,
        description,
        link,
        pubDate: new Date(pubDateRaw).toISOString(),
        source: source.id,
        sourceName: source.name,
        sourceFullName: source.fullName,
        sourceColor: source.color,
        category: category || "General",
        topics: detectTopics(title, description),
        imageUrl,
      };
    }).filter(Boolean);
  } catch (e) {
    console.error(`Feed error [${source.id} - ${url}]:`, e.message);
    return [];
  }
}

exports.handler = async (event) => {
  const corsHeaders = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Cache-Control": "public, max-age=300",
  };

  if (event.httpMethod === "OPTIONS") {
    return { statusCode: 204, headers: corsHeaders, body: "" };
  }

  try {
    const allPromises = RSS_SOURCES.flatMap(source =>
      source.feeds.map(url => parseFeed(url, source))
    );
    const results = await Promise.allSettled(allPromises);

    let allArticles = [];
    results.forEach(r => { if (r.status === "fulfilled") allArticles = allArticles.concat(r.value); });

    // Deduplicate
    const seen = new Set();
    const deduped = allArticles.filter(a => {
      if (!a || !a.title) return false;
      const key = a.title.toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 60);
      if (seen.has(key)) return false;
      seen.add(key); return true;
    });

    deduped.sort((a, b) => new Date(b.pubDate) - new Date(a.pubDate));

    const grouped = {};
    RSS_SOURCES.forEach(s => { grouped[s.id] = []; });
    deduped.forEach(a => { if (grouped[a.source]) grouped[a.source].push(a); });

    const topicGrouped = {};
    deduped.forEach(a => {
      a.topics.forEach(t => {
        if (!topicGrouped[t]) topicGrouped[t] = [];
        topicGrouped[t].push(a);
      });
    });

    return {
      statusCode: 200,
      headers: corsHeaders,
      body: JSON.stringify({
        articles: deduped.slice(0, 250),
        grouped,
        topicGrouped,
        sources: RSS_SOURCES.map(s => ({ id: s.id, name: s.name, fullName: s.fullName, color: s.color })),
        topics: Object.keys(UPSC_TOPICS),
        lastUpdated: new Date().toISOString(),
        total: deduped.length,
      }),
    };
  } catch (error) {
    return {
      statusCode: 500,
      headers: corsHeaders,
      body: JSON.stringify({ error: error.message }),
    };
  }
};
