#!/usr/bin/env python3
"""Fetch the newest University of Florida baseball stories for the digest."""
from __future__ import annotations

import email.utils
import html as html_lib
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) UFBaseballDigest/1.0"}
SITES = [
    {"name": "Florida Gators", "url": "https://floridagators.com/sports/baseball", "query": "site:floridagators.com baseball", "keywords": ("baseball", "mlb", "bowen", "strayer", "cyr", "mcneillie", "yost", "sandefer", "peterson", "dukes"), "logo": "https://floridagators.com/favicon.ico", "kind": "google_news", "accent": "#fa4616"},
    {"name": "WRUF", "url": "https://www.wruf.com/", "query": "site:wruf.com Florida baseball", "keywords": ("baseball", "mlb", "bowen", "strayer", "cyr", "mcneillie", "yost", "sandefer", "peterson"), "logo": "https://www.wruf.com/favicon.ico", "kind": "google_news", "accent": "#fa4616"},
    {"name": "Gator Country", "url": "https://www.gatorcountry.com/florida-gators-baseball/", "feed": "https://www.gatorcountry.com/florida-gators-baseball/feed/", "logo": "https://www.gatorcountry.com/favicon.ico", "kind": "feed", "accent": "#0021a5"},
    {"name": "Florida Gators on SI", "url": "https://www.si.com/college/florida", "query": "site:si.com/college/florida baseball", "keywords": ("baseball", "mlb", "bowen", "caglianone", "mcneillie", "peterson", "cyr"), "logo": "https://www.si.com/favicon.ico", "kind": "google_news", "accent": "#0021a5"},
    {"name": "Gainesville Sun", "url": "https://www.gainesville.com/", "query": "site:gainesville.com Florida baseball", "keywords": ("baseball", "mlb", "surowiec", "peterson", "draft"), "logo": "https://www.gainesville.com/favicon.ico", "kind": "google_news", "accent": "#4b5563"},
]


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=35) as response:
        return response.read().decode("utf-8", "replace")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_lib.unescape(text or ""))).strip()


def date_value(raw: str) -> str:
    raw = (raw or "").strip()
    try:
        return email.utils.parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    return raw


def image_from_item(item: ET.Element, description: str) -> str:
    for element in item.iter():
        if element.tag.lower().endswith(("thumbnail", "content")):
            value = element.attrib.get("url") or element.attrib.get("href")
            if value and value.startswith("http"):
                return value
    match = re.search(r"<img[^>]+src=[\"']([^\"']+)", description or "", re.I)
    return match.group(1) if match else ""


def feed_posts(site: dict) -> list[dict]:
    root = ET.fromstring(fetch(site["feed"]))
    posts = []
    for item in [element for element in root.iter() if element.tag.lower().endswith(("item", "entry"))]:
        values = {}
        for element in list(item):
            values.setdefault(element.tag.rsplit("}", 1)[-1].lower(), element.text or "")
        title = clean(values.get("title", "Untitled"))
        link = values.get("link", "")
        if not link:
            link = next((element.attrib["href"] for element in list(item) if element.tag.lower().endswith("link") and element.attrib.get("href")), "")
        description = values.get("description") or values.get("summary") or values.get("encoded") or ""
        posts.append({"title": title, "url": link.strip(), "published": date_value(values.get("pubdate") or values.get("published") or values.get("updated") or values.get("date") or ""), "excerpt": clean(description)[:220], "image": image_from_item(item, description)})
        if len(posts) == 5:
            break
    return posts


def google_news_posts(site: dict) -> list[dict]:
    query = urllib.parse.urlencode({"q": f"{site['query']} when:90d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    root = ET.fromstring(fetch(f"https://news.google.com/rss/search?{query}"))
    posts = []
    for item in root.findall(".//item"):
        title = clean(item.findtext("title") or "Untitled")
        if not any(word in title.lower() for word in site["keywords"]):
            continue
        posts.append({"title": re.sub(r"\s+-\s+[^-]+$", "", title), "url": (item.findtext("link") or "").strip(), "published": date_value(item.findtext("pubDate") or ""), "excerpt": clean(item.findtext("description") or "")[:220], "image": ""})
        if len(posts) == 5:
            break
    return posts


def collect(site: dict) -> dict:
    posts = google_news_posts(site) if site["kind"] == "google_news" else feed_posts(site)
    if len(posts) < 5:
        raise RuntimeError(f"only parsed {len(posts)} relevant posts")
    return {**site, "posts": posts, "status": "ok"}


def main() -> None:
    by_name, errors = {}, []
    with ThreadPoolExecutor(max_workers=len(SITES)) as pool:
        futures = {pool.submit(collect, site): site for site in SITES}
        for future in as_completed(futures):
            site = futures[future]
            try:
                by_name[site["name"]] = future.result()
            except Exception as exc:
                errors.append(f"{site['name']}: {exc}")
                by_name[site["name"]] = {**site, "posts": [], "status": "error", "error": str(exc)}
    results = [by_name[site["name"]] for site in SITES]
    OUT.write_text(json.dumps({"refreshedAt": datetime.now().astimezone().isoformat(), "sites": results, "errors": errors}, ensure_ascii=False, indent=2))
    print(json.dumps({"sites": len(results), "successful": len(results) - len(errors), "errors": errors}))
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
