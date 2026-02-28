#!/usr/bin/env python3
"""
PIB Scraper for UPSC Samachar (Netlify-safe)

Why items were empty:
- Netlify often fails to fetch/parse PIB HTML listing OR RSS due to encoding/blocking.
This script uses a robust strategy:
1) Try multiple PIB RSS feeds (most reliable).
2) If RSS returns 0, fallback to PIB HTML listing pages.
3) Fetch each PRID detail page and generate JSON files.

Outputs:
- public/data/pib_index.json
- public/data/items/<PRID>.json
"""

import os
import re
import sys
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed


# ── CONFIG ────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(__file__)
OUT_DIR       = os.path.join(BASE_DIR, "public", "data")
ITEMS_DIR     = os.path.join(OUT_DIR, "items")

MAX_ARTICLES  = 60
MAX_WORKERS   = 6
TIMEOUT       = 25
DELAY_BETWEEN = 0.35

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",  # do NOT include br unless brotli installed
    "Connection": "keep-alive",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# UPSC-priority ministry list
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


def detect_topics(text: str):
    t = (text or "").lower()
    matched = [topic for topic, kws in TOPIC_KEYWORDS.items() if any(kw in t for kw in kws)]
    return matched[:3] if matched else ["General"]


def fetch(url: str, referer: str | None = None) -> str | None:
    """Fetch URL with retry."""
    hdrs = {}
    if referer:
        hdrs["Referer"] = referer

    for attempt in range(3):
        try:
            r = SESSION.get(url, headers=hdrs, timeout=TIMEOUT)
            if r.status_code == 200:
                r.encoding = r.apparent_encoding or "utf-8"
                return r.text
            print(f"HTTP {r.status_code} -> {url}", flush=True)
        except Exception as e:
            print(f"Fetch error attempt {attempt+1} -> {url}: {e}", flush=True)

        time.sleep(1.2 * (attempt + 1))

    return None


# ── Listing: RSS ──────────────────────────────────────────────────
def list_prids_from_rss(rss_url: str):
    print(f"[RSS] {rss_url}", flush=True)
    xml_text = fetch(rss_url, referer="https://www.pib.gov.in/")
    if not xml_text:
        return []

    # clean any junk before XML tag
    xml_text = xml_text.strip()
    if "<" in xml_text:
        xml_text = xml_text[xml_text.find("<"):]

    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        print(f"[RSS] parse error: {e}", flush=True)
        return []

    results = []
    seen = set()

    for item in root.findall(".//item"):
        link = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        title = re.sub(r"\s+", " ", title).strip()

        m = re.search(r"PRID=(\d{6,8})", link, re.I)
        if not m:
            continue

        prid = m.group(1)
        if prid in seen:
            continue
        seen.add(prid)

        # skip Hindi titles
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

    print(f"[RSS] found {len(results)} PRIDs", flush=True)
    return results


# ── Listing: HTML fallback ────────────────────────────────────────
def list_prids_from_html(listing_url: str):
    print(f"[HTML] {listing_url}", flush=True)
    html = fetch(listing_url, referer="https://www.pib.gov.in/")
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()

    for a_tag in soup.find_all("a", href=re.compile(r"PRID=\d{6,8}", re.I)):
        href = a_tag.get("href", "")
        m = re.search(r"PRID=(\d{6,8})", href, re.I)
        if not m:
            continue

        prid = m.group(1)
        if prid in seen:
            continue
        seen.add(prid)

        title = a_tag.get_text(" ", strip=True)
        title = re.sub(r"\s+", " ", title).strip() or a_tag.get("title", "").strip()

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

    print(f"[HTML] found {len(results)} PRIDs", flush=True)
    return results


def _make_article(prid, title, ministry, pub_date, snippet, url):
    return {
        "prid": prid,
        "title": title,
        "ministry": ministry,
        "snippet": snippet,
        "source_url": url,
        "pub_date": pub_date,
        "posted_on_raw": "",
        "pdfs": [],
        "topics": detect_topics((title or "") + " " + (snippet or "")),
    }


def parse_detail_page(prid, inline_title="", inline_ministry="", inline_date=""):
    url = f"https://pib.gov.in/PressReleasePage.aspx?PRID={prid}"
    html = fetch(url, referer="https://www.pib.gov.in/")
    if not html:
        if inline_title and len(inline_title) >= 10:
            return _make_article(prid, inline_title, inline_ministry, inline_date, "", url), ""
        return None, None

    soup = BeautifulSoup(html, "lxml")

    # Title
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
            t = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
            if len(t) >= 12 and not re.search(r"[\u0900-\u097F]{5,}", t):
                title = t
                break

    if not title:
        og = soup.find("meta", {"property": "og:title"}) or soup.find("meta", {"name": "title"})
        if og and og.get("content"):
            title = og["content"].strip()

    if not title:
        title = inline_title or ""

    if not title or len(title) < 8 or re.search(r"[\u0900-\u097F]{8,}", title):
        return None, None

    # Ministry
    ministry = inline_ministry
    if not ministry:
        page_text = soup.get_text(" ")
        for min_name in MINISTRY_ORDER:
            if min_name in page_text:
                ministry = min_name
                break

    # Date
    date_str = inline_date
    if not date_str:
        dm = re.search(r"Posted\s+On[:\s]*(\d{1,2}\s+\w+\s+\d{4})", soup.get_text(" "), re.I)
        if dm:
            date_str = dm.group(1)

    pub_date = datetime.now(timezone.utc).isoformat()
    if date_str:
        for fmt in ["%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y"]:
            try:
                pub_date = datetime.strptime(date_str.strip(), fmt).replace(tzinfo=timezone.utc).isoformat()
                break
            except ValueError:
                pass

    # Body
    body_text = ""
    for selector in ["div#ContentDiv", "div.content-area", "main", "div#content"]:
        el = soup.select_one(selector)
        if el:
            for tag in el(["script", "style", "noscript"]):
                tag.decompose()
            body_text = el.get_text("\n", strip=True)
            body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()
            if len(body_text) > 100:
                break

    snippet = " ".join(body_text.split())[:500] if body_text else ""

    # PDFs
    pdfs = []
    for a in soup.find_all("a", href=re.compile(r"\.pdf", re.I)):
        href = a.get("href", "")
        if href and not href.startswith("http"):
            href = "https://pib.gov.in" + href
        if href:
            label = a.get_text(strip=True) or "PDF"
            pdfs.append({"url": href, "label": label[:80]})

    article = _make_article(prid, title, ministry, pub_date, snippet, url)
    article["pdfs"] = pdfs
    article["posted_on_raw"] = date_str or ""

    return article, body_text


def write_empty_index():
    out = {"updated_at_utc": datetime.now(timezone.utc).isoformat(), "items": []}
    with open(os.path.join(OUT_DIR, "pib_index.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(ITEMS_DIR, exist_ok=True)

    print("=" * 60, flush=True)
    print("PIB SCRAPER — UPSC Samachar", flush=True)
    print(f"UTC Time: {datetime.now(timezone.utc).isoformat()}", flush=True)
    print("=" * 60, flush=True)

    # 1) Try RSS feeds (multiple variants)
    rss_pages = [
        "https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
        "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
        "https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=1",
        "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=1",
        "https://archive.pib.gov.in/newsite/rssenglish.aspx",
    ]

    entries = []
    for u in rss_pages:
        got = list_prids_from_rss(u)
        for e in got:
            entries.append(e)
        if len(entries) >= MAX_ARTICLES:
            break
        time.sleep(DELAY_BETWEEN)

    # de-dup
    uniq = []
    seen = set()
    for e in entries:
        if e["prid"] not in seen:
            seen.add(e["prid"])
            uniq.append(e)
        if len(uniq) >= MAX_ARTICLES:
            break

    # 2) Fallback to HTML listing if RSS gave nothing
    if not uniq:
        listing_pages = [
            "https://pib.gov.in/Allrel.aspx?lang=1&reg=3",
            "https://pib.gov.in/PMContents/PMContents.aspx?menuid=1&Lang=1&RegionId=3",
        ]
        for u in listing_pages:
            got = list_prids_from_html(u)
            for e in got:
                if e["prid"] not in seen:
                    seen.add(e["prid"])
                    uniq.append(e)
                if len(uniq) >= MAX_ARTICLES:
                    break
            if len(uniq) >= MAX_ARTICLES:
                break
            time.sleep(DELAY_BETWEEN)

    print(f"Total unique PRIDs to fetch: {len(uniq)}", flush=True)

    if not uniq:
        print("ERROR: No PRIDs found via RSS or HTML. PIB is likely blocking Netlify.", flush=True)
        write_empty_index()
        sys.exit(0)

    # 3) Fetch detail pages in parallel
    articles = []
    failed = 0

    def fetch_one(entry):
        art, full_text = parse_detail_page(
            entry["prid"],
            entry.get("inline_title", ""),
            entry.get("inline_ministry", ""),
            entry.get("inline_date", ""),
        )
        return entry["prid"], art, full_text

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_one, e): e for e in uniq}
        done = 0
        for future in as_completed(futures):
            prid, art, full_text = future.result()
            done += 1
            if art:
                articles.append(art)
                item_path = os.path.join(ITEMS_DIR, f"{prid}.json")
                with open(item_path, "w", encoding="utf-8") as f:
                    json.dump({"prid": prid, "text": full_text or ""}, f, ensure_ascii=False)
                print(f"[{done}/{len(uniq)}] ✓ {prid}: {art['title'][:70]}", flush=True)
            else:
                failed += 1
                print(f"[{done}/{len(uniq)}] ✗ {prid}: skipped", flush=True)
            time.sleep(0.08)

    # Sort newest first
    articles.sort(key=lambda a: a.get("pub_date", ""), reverse=True)

    output = {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "total": len(articles),
        "items": articles,
    }

    out_path = os.path.join(OUT_DIR, "pib_index.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("=" * 60, flush=True)
    print(f"✅ Done! Articles written: {len(articles)}", flush=True)
    print(f"✗ Failed/skipped: {failed}", flush=True)
    print(f"Output: {out_path}", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()