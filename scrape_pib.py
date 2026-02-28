#!/usr/bin/env python3
"""
PIB Scraper for UPSC Samachar (Netlify-friendly)

Fix:
- Instead of scraping pib.gov.in HTML listing pages (often blocked/encoded on Netlify),
  this version reads PIB's official RSS feed to get PRIDs reliably.

Flow:
1) Read PIB RSS -> collect PRIDs (MAX_ARTICLES)
2) Fetch each PRID detail page -> parse title/date/ministry/body
3) Write:
   - public/data/pib_index.json
   - public/data/items/<PRID>.json

Run: python scrape_pib.py
Netlify runs this automatically via build command in netlify.toml
"""

import requests
import re
import json
import os
import time
import sys
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET


# ── CONFIG ────────────────────────────────────────────────────────
OUT_DIR       = os.path.join(os.path.dirname(__file__), "public", "data")
ITEMS_DIR     = os.path.join(OUT_DIR, "items")
MAX_ARTICLES  = 60          # how many PIB releases to fetch
MAX_WORKERS   = 6           # parallel fetches
TIMEOUT       = 20          # seconds per request
DELAY_BETWEEN = 0.3         # seconds between batches (be polite)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    # RSS sometimes returns as XML; keep Accept broad
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    # IMPORTANT: do NOT ask for br (brotli) unless brotli installed
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# UPSC-priority ministry list (same as pibdigest)
MINISTRY_ORDER = [
    "Prime Minister's Office",
    "Ministry of Defence",
    "Ministry of Home Affairs",
    "Ministry of External Affairs",
    "Ministry of Finance",
    "Ministry of Law and Justice",
    "Ministry of Environment, Forest and Climate Change",
    "Ministry of Health and Family Welfare",
    "Ministry of Education",
    "Ministry of Agriculture & Farmers Welfare",
    "Ministry of Railways",
    "Ministry of Road Transport and Highways",
    "Ministry of Power",
    "Ministry of Petroleum and Natural Gas",
    "Ministry of Commerce and Industry",
    "Ministry of Electronics & IT",
    "Ministry of Science & Technology",
    "Ministry of Labour & Employment",
    "Ministry of Rural Development",
    "Ministry of Housing and Urban Affairs",
    "Ministry of Women and Child Development",
    "Ministry of Social Justice and Empowerment",
    "Ministry of Tribal Affairs",
    "Ministry of Consumer Affairs, Food and Public Distribution",
    "Ministry of Parliamentary Affairs",
    "Ministry of Civil Aviation",
    "Ministry of Coal",
    "Ministry of Heavy Industries",
    "Ministry of Panchayati Raj",
    "Ministry of Jal Shakti",
    "Ministry of Information & Broadcasting",
    "Ministry of Ports, Shipping and Waterways",
    "Ministry of Tourism",
    "Ministry of Culture",
    "Ministry of Youth Affairs & Sports",
    "Ministry of Steel",
    "Ministry of Mines",
    "Ministry of New and Renewable Energy",
    "Ministry of Fisheries, Animal Husbandry and Dairying",
    "Ministry of Cooperation",
    "NITI Aayog",
    "Cabinet Secretariat",
    "President's Secretariat",
    "Election Commission of India",
]

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def fetch(url, referer=None):
    """Fetch a URL with retry logic."""
    hdrs = {}
    if referer:
        hdrs["Referer"] = referer
    for attempt in range(3):
        try:
            r = SESSION.get(url, headers=hdrs, timeout=TIMEOUT)
            if r.status_code == 200:
                r.encoding = r.apparent_encoding or "utf-8"
                return r.text
            print(f"  HTTP {r.status_code} for {url}", flush=True)
        except Exception as e:
            print(f"  Attempt {attempt+1} failed for {url}: {e}", flush=True)
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    return None


# ── NEW: RSS listing (replaces HTML listing) ──────────────────────
def scrape_listing_page(rss_url):
    """
    Read PIB RSS and return list of entries:
      { prid, inline_title, inline_ministry, inline_date }
    inline_ministry/date may be empty — detail page parsing will fill them.
    """
    print(f"Scraping RSS: {rss_url}", flush=True)
    xml_text = fetch(rss_url, referer="https://www.pib.gov.in/")
    if not xml_text:
        return []

    results = []
    seen = set()

    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        print(f"  RSS parse error: {e}", flush=True)
        return []

    # Standard RSS: channel/item
    for item in root.findall(".//item"):
        link = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()

        m = re.search(r"PRID=(\d{6,8})", link, re.I)
        if not m:
            continue

        prid = m.group(1)
        if prid in seen:
            continue
        seen.add(prid)

        title = re.sub(r"\s+", " ", title).strip()

        # Skip Hindi-heavy titles
        if re.search(r"[\u0900-\u097F]{5,}", title):
            continue

        results.append({
            "prid": prid,
            "inline_title": title,
            "inline_ministry": "",
            "inline_date": "",
        })

        if len(results) >= MAX_ARTICLES:
            break

    print(f"  Found {len(results)} PRIDs via RSS", flush=True)
    return results


def parse_detail_page(prid, inline_title="", inline_ministry="", inline_date=""):
    """Fetch and parse a single PIB press release page."""
    url = f"https://pib.gov.in/PressReleasePage.aspx?PRID={prid}"
    html = fetch(url, referer="https://www.pib.gov.in/")

    if not html:
        # Fallback to inline data
        if inline_title and len(inline_title) >= 10:
            return _make_article(prid, inline_title, inline_ministry, inline_date, "", url), ""
        return None, None

    soup = BeautifulSoup(html, "lxml")

    # ── Title ──────────────────────────────────────────────────
    title = ""
    for selector in [
        "div.innner-page-main-about-us-head-right h2",
        "div.innner-page-main-about-us-head-right h3",
        "div#ContentDiv h2",
        "div#ContentDiv h3",
        "h2.page-title",
        "h1",
        "h2",
    ]:
        el = soup.select_one(selector)
        if el:
            t = el.get_text(" ", strip=True)
            t = re.sub(r"\s+", " ", t).strip()
            if len(t) >= 15 and not re.search(r"[\u0900-\u097F]{5,}", t):
                title = t
                break

    if not title:
        og = soup.find("meta", {"property": "og:title"}) or soup.find("meta", {"name": "title"})
        if og and og.get("content"):
            title = og["content"].strip()

    if not title and inline_title:
        title = inline_title

    if not title or len(title) < 8:
        return None, None

    # Skip Hindi releases
    if re.search(r"[\u0900-\u097F]{8,}", title):
        return None, None

    # ── Ministry ───────────────────────────────────────────────
    ministry = inline_ministry
    if not ministry:
        page_text = soup.get_text(" ")
        for min_name in MINISTRY_ORDER:
            if min_name in page_text:
                ministry = min_name
                break

    # ── Date ───────────────────────────────────────────────────
    date_str = inline_date
    if not date_str:
        for pat in [
            r"Posted\s+On[:\s]*(\d{1,2}\s+\w+\s+\d{4})",
            r"(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})",
        ]:
            dm = re.search(pat, soup.get_text(" "), re.I)
            if dm:
                date_str = dm.group(1)
                break

    pub_date = datetime.now(timezone.utc).isoformat()
    if date_str:
        for fmt in ["%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y"]:
            try:
                pub_date = datetime.strptime(date_str.strip(), fmt).replace(tzinfo=timezone.utc).isoformat()
                break
            except ValueError:
                continue

    # ── Body text ──────────────────────────────────────────────
    body_text = ""
    for selector in [
        "div#ContentDiv",
        "div.innner-page-main-about-us-head-right",
        "div.content-area",
        "div#content",
        "main",
    ]:
        el = soup.select_one(selector)
        if el:
            for tag in el(["script", "style", "noscript"]):
                tag.decompose()
            body_text = el.get_text("\n", strip=True)
            body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()
            if len(body_text) > 100:
                break

    snippet = " ".join(body_text.split())[:500] if body_text else ""

    # ── PDF links ─────────────────────────────────────────────
    pdfs = []
    for a in soup.find_all("a", href=re.compile(r"\.pdf", re.I)):
        href = a.get("href", "")
        if not href.startswith("http"):
            href = "https://pib.gov.in" + href
        label = a.get_text(strip=True) or "PDF"
        pdfs.append({"url": href, "label": label[:80]})

    article = _make_article(prid, title, ministry, pub_date, snippet, url)
    article["pdfs"] = pdfs
    article["posted_on_raw"] = date_str or ""

    return article, body_text


def _make_article(prid, title, ministry, pub_date, snippet, url):
    """Build a normalized article dict."""
    return {
        "prid": prid,
        "title": title,
        "ministry": ministry,
        "snippet": snippet,
        "source_url": url,
        "pub_date": pub_date,
        "posted_on_raw": "",
        "pdfs": [],
        "topics": detect_topics(title + " " + snippet),
    }


TOPIC_KEYWORDS = {
    "Polity & Governance": ["parliament","constitution","supreme court","high court","election","amendment","bill","act","cabinet","president","governor","lok sabha","rajya sabha","judiciary","panchayat","governance","policy","commission","ordinance","regulation","tribunal"],
    "Economy": ["gdp","inflation","rbi","sebi","budget","fiscal","monetary","repo rate","economy","trade","export","import","fdi","msme","agriculture","msp","niti aayog","economic","tax","gst","growth","market","rupee","investment","revenue","finance","bank","insurance","credit"],
    "Environment & Ecology": ["climate","biodiversity","forest","wildlife","pollution","carbon","emission","renewable","solar","ozone","ramsar","tiger","elephant","coral","wetland","deforestation","net zero","cop","ecology","conservation","environment","water","river","drought","flood","green"],
    "Science & Technology": ["isro","space","satellite","artificial intelligence","quantum","nuclear","research","technology","5g","semiconductor","drone","cyber","digital","blockchain","genomics","innovation","patent","rocket","launch","mission","ai","iit","csir","dst"],
    "International Relations": ["bilateral","treaty","summit","united nations","world bank","imf","wto","g20","brics","sco","asean","nato","geopolitics","diplomacy","foreign","sanctions","agreement","alliance","visit","memorandum","mou","quad","g7","un security"],
    "Social Issues": ["poverty","welfare","education","health","nutrition","women","child","tribal","dalit","minority","reservation","disability","elderly","hunger","literacy","inequality","yojana","scheme","programme","social security","pm-kisan"],
    "Defence & Security": ["defence","military","army","navy","air force","border","security","terrorism","naxal","insurgency","weapon","missile","drdo","iaf","coast guard","exercise","combat","strategic","bsf","crpf"],
    "Infrastructure & Development": ["railway","highway","port","airport","metro","smart city","urban","housing","construction","energy","power","grid","infrastructure","expressway","corridor","project","bridge","dam","roads","nhsrcl"],
}

def detect_topics(text):
    text_lower = text.lower()
    matched = [topic for topic, kws in TOPIC_KEYWORDS.items() if any(kw in text_lower for kw in kws)]
    return matched[:3] if matched else ["General"]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(ITEMS_DIR, exist_ok=True)

    print("=" * 60, flush=True)
    print("PIB SCRAPER — UPSC Samachar (RSS mode)", flush=True)
    print(f"Time: {datetime.now(timezone.utc).isoformat()}", flush=True)
    print("=" * 60, flush=True)

    # ── Step 1: Collect PRIDs from RSS ─────────────────────────
    # PIB "Press Releases RSS" commonly works well on servers like Netlify
    rss_pages = [
        "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=1",
    ]

    all_entries = []
    seen_prids = set()

    for rss_url in rss_pages:
        entries = scrape_listing_page(rss_url)
        for e in entries:
            if e["prid"] not in seen_prids:
                seen_prids.add(e["prid"])
                all_entries.append(e)
        if len(all_entries) >= MAX_ARTICLES:
            break
        time.sleep(DELAY_BETWEEN)

    all_entries = all_entries[:MAX_ARTICLES]
    print(f"\nTotal unique PRIDs to fetch: {len(all_entries)}", flush=True)

    if not all_entries:
        print("ERROR: No PRIDs found! RSS may be blocked or empty.", flush=True)
        out = {"updated_at_utc": datetime.now(timezone.utc).isoformat(), "items": []}
        with open(os.path.join(OUT_DIR, "pib_index.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        sys.exit(0)

    # ── Step 2: Fetch each press release page (parallel) ──────
    articles = []
    failed = 0

    def fetch_one(entry):
        art, full_text = parse_detail_page(
            entry["prid"],
            entry["inline_title"],
            entry["inline_ministry"],
            entry["inline_date"],
        )
        return entry["prid"], art, full_text

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_one, e): e for e in all_entries}
        done = 0
        for future in as_completed(futures):
            prid, art, full_text = future.result()
            done += 1
            if art:
                articles.append(art)
                item_path = os.path.join(ITEMS_DIR, f"{prid}.json")
                with open(item_path, "w", encoding="utf-8") as f:
                    json.dump({"prid": prid, "text": full_text or ""}, f, ensure_ascii=False)
                print(f"  [{done}/{len(all_entries)}] ✓ {prid}: {art['title'][:60]}", flush=True)
            else:
                failed += 1
                print(f"  [{done}/{len(all_entries)}] ✗ {prid}: skipped", flush=True)
            time.sleep(0.1)

    # Sort by pub_date descending
    articles.sort(key=lambda a: a.get("pub_date", ""), reverse=True)

    # ── Step 3: Write pib_index.json ──────────────────────────
    output = {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "total": len(articles),
        "items": articles,
    }

    out_path = os.path.join(OUT_DIR, "pib_index.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60, flush=True)
    print(f"✅ Done! {len(articles)} articles written to {out_path}", flush=True)
    print(f"✗ Failed/skipped: {failed}", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()