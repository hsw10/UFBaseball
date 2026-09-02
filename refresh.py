#!/usr/bin/env python3
"""Build a newest-first, cross-source Florida Gators baseball digest."""
from __future__ import annotations

import email.utils
import html as html_lib
import json
import re
import ssl
import sys
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) UFBaseballDigest/2.0"}
SSL_CONTEXT = ssl.create_default_context()
POST_COUNT = 25
POSTS_PER_SOURCE = 12
AFFILIATION_TERMS = ("florida", "gator", "sully", "o'sullivan", "caglianone", "peterson", "cyr", "mcneillie", "yost", "bowen", "sandefer", "surowiec", "walls")

# All sources requested for the digest. Native feeds are preferred where available;
# Google News site searches are a documented fallback for sites without usable feeds.
SITES = [
    {"name": "Florida Gators", "url": "https://floridagators.com/sports/baseball", "query": "site:floridagators.com baseball", "kind": "news", "keywords": ("baseball", "mlb")},
    {"name": "GatorCountry", "url": "https://www.gatorcountry.com/florida-gators-baseball/", "feed": "https://www.gatorcountry.com/florida-gators-baseball/feed/", "kind": "feed", "keywords": ()},
    {"name": "Gators Wire", "url": "https://gatorswire.usatoday.com/", "query": "site:gatorswire.usatoday.com Florida baseball", "kind": "news", "keywords": ("baseball", "mlb")},
    {"name": "Alligator Army", "url": "https://www.alligatorarmy.com/", "query": "site:alligatorarmy.com Florida baseball", "kind": "news", "keywords": ("baseball", "mlb")},
    {"name": "WRUF Sports", "url": "https://www.wruf.com/headlines/category/sports/gators/baseball/", "feed": "https://www.wruf.com/headlines/category/baseball/feed/", "kind": "feed", "keywords": ("gator", "florida", "baseball", "peterson", "cyr", "mcneillie", "yost", "bowen", "sandefer", "walls")},
    {"name": "Swamp247", "url": "https://247sports.com/college/florida/", "query": "site:247sports.com/college/florida Florida baseball", "kind": "news", "keywords": ("baseball", "mlb")},
    {"name": "Gators Online", "url": "https://www.on3.com/teams/florida-gators/", "query": "site:on3.com/teams/florida-gators Florida baseball", "kind": "news", "keywords": ("baseball", "mlb")},
    {"name": "The Independent Florida Alligator", "url": "https://www.alligator.org/section/sports", "query": "site:alligator.org Florida baseball", "kind": "news", "keywords": ("baseball", "mlb")},
    {"name": "D1Baseball", "url": "https://d1baseball.com/team/florida/", "query": "site:d1baseball.com Florida Gators baseball", "kind": "news", "keywords": ("florida", "gators", "baseball", "mlb")},
    {"name": "Baseball America", "url": "https://www.baseballamerica.com/teams/1199-florida-gators/", "query": "site:baseballamerica.com Florida baseball", "kind": "news", "keywords": ("florida", "gators", "baseball", "mlb")},
    {"name": "GatorSports", "url": "https://www.gatorsports.com/", "query": "site:gatorsports.com Florida baseball", "kind": "news", "keywords": ("florida", "gators", "baseball", "mlb")},
    {"name": "Florida Times-Union", "url": "https://www.jacksonville.com/", "query": "site:jacksonville.com Florida Gators baseball", "kind": "news", "keywords": ("florida", "gators", "baseball", "o'sullivan", "draft")},
    {"name": "Hail Florida Hail", "url": "https://hailfloridahail.com/", "query": "site:hailfloridahail.com Florida Gators baseball", "kind": "news", "keywords": ("florida", "gators", "baseball", "sully", "o'sullivan", "draft")},
]


def fetch(url: str) -> tuple[str, str]:
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=35, context=SSL_CONTEXT) as response:
            return response.read().decode("utf-8", "replace"), response.geturl()
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc.reason):
            raise
        with urllib.request.urlopen(request, timeout=35, context=ssl._create_unverified_context()) as response:
            return response.read().decode("utf-8", "replace"), response.geturl()


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


def timestamp(post: dict) -> float:
    try:
        return datetime.fromisoformat(post["published"].replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def relevant_title(title: str, keywords: tuple[str, ...]) -> bool:
    """Keep Florida/Gators baseball coverage, excluding other Florida programs."""
    lowered = title.lower()
    return any(word in lowered for word in keywords) and any(term in lowered for term in AFFILIATION_TERMS)


def image_from_item(item: ET.Element, description: str) -> str:
    for element in item.iter():
        if element.tag.lower().endswith(("thumbnail", "content")):
            value = element.attrib.get("url") or element.attrib.get("href")
            if value and value.startswith("http"):
                return value
    match = re.search(r"<img[^>]+src=[\"']([^\"']+)", description or "", re.I)
    return html_lib.unescape(match.group(1)) if match else ""


def feed_posts(site: dict) -> list[dict]:
    xml, _ = fetch(site["feed"])
    root = ET.fromstring(xml)
    posts = []
    for item in [element for element in root.iter() if element.tag.lower().endswith(("item", "entry"))]:
        values = {}
        for element in list(item):
            values.setdefault(element.tag.rsplit("}", 1)[-1].lower(), element.text or "")
        title = clean(values.get("title", "Untitled"))
        description = values.get("description") or values.get("summary") or values.get("encoded") or ""
        if site["keywords"] and not relevant_title(title, site["keywords"]):
            continue
        link = values.get("link", "") or next((element.attrib["href"] for element in list(item) if element.tag.lower().endswith("link") and element.attrib.get("href")), "")
        posts.append({"title": title, "url": link.strip(), "published": date_value(values.get("pubdate") or values.get("published") or values.get("updated") or values.get("date") or ""), "excerpt": clean(description)[:220], "image": image_from_item(item, description), "source": site["name"], "sourceUrl": site["url"]})
        if len(posts) == POSTS_PER_SOURCE:
            break
    return posts


def news_posts(site: dict) -> list[dict]:
    params = urllib.parse.urlencode({"q": f"{site['query']} when:365d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    xml, _ = fetch(f"https://news.google.com/rss/search?{params}")
    posts = []
    for item in ET.fromstring(xml).findall(".//item"):
        raw_title = clean(item.findtext("title") or "Untitled")
        title = re.sub(r"\s+-\s+[^-]+$", "", raw_title)
        if not relevant_title(title, site["keywords"]):
            continue
        posts.append({"title": title, "url": (item.findtext("link") or "").strip(), "published": date_value(item.findtext("pubDate") or ""), "excerpt": clean(item.findtext("description") or "")[:220], "image": "", "source": site["name"], "sourceUrl": site["url"]})
        if len(posts) == POSTS_PER_SOURCE:
            break
    return posts


def article_image(post: dict) -> dict:
    if post["image"] or not post["url"]:
        return post
    try:
        page, resolved = fetch(post["url"])
        match = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)', page, re.I)
        if not match:
            match = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']', page, re.I)
        post["url"] = resolved
        post["image"] = html_lib.unescape(match.group(1)) if match else ""
    except Exception:
        pass
    return post


def collect(site: dict) -> dict:
    posts = feed_posts(site) if site["kind"] == "feed" else news_posts(site)
    return {**site, "posts": posts, "status": "ok"}


def main() -> None:
    sources, errors = [], []
    with ThreadPoolExecutor(max_workers=len(SITES)) as pool:
        futures = {pool.submit(collect, site): site for site in SITES}
        for future in as_completed(futures):
            site = futures[future]
            try:
                sources.append(future.result())
            except Exception as exc:
                errors.append(f"{site['name']}: {exc}")
                sources.append({**site, "posts": [], "status": "error", "error": str(exc)})
    sources.sort(key=lambda source: [site["name"] for site in SITES].index(source["name"]))
    seen, candidates = set(), []
    for source in sources:
        for post in source["posts"]:
            key = re.sub(r"[^a-z0-9]+", "", post["title"].lower())
            if key not in seen:
                seen.add(key)
                candidates.append(post)
    candidates.sort(key=timestamp, reverse=True)
    newest = candidates[:POST_COUNT]
    with ThreadPoolExecutor(max_workers=10) as pool:
        newest = list(pool.map(article_image, newest))
    payload = {"refreshedAt": datetime.now().astimezone().isoformat(), "posts": newest, "sources": sources, "errors": errors}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps({"sources": len(sources), "successful": len(sources) - len(errors), "posts": len(newest), "withImages": sum(bool(post["image"]) for post in newest), "errors": errors}))
    if len(newest) < POST_COUNT:
        sys.exit(1)


if __name__ == "__main__":
    main()