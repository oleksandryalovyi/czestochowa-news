#!/usr/bin/env python3
"""Poll every configured feed and upsert new articles into the store.

Run hourly from launchd. Designed never to raise: a feed that is down, rate
limited or serving malformed XML is logged and skipped so the other three
still get collected.
"""

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import timezone

import netutil
import scrape_naszemiasto
import store
from feeds import FEEDS
from netutil import clean_text, http_get, log, normalise_link, title_hash

DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

# og:description / <meta name="description"> on the article page, used only
# when a feed gives us nothing usable. Not full-article scraping — one small
# request per genuinely new item.
META_DESC_RE = re.compile(
    rb"""<meta[^>]+(?:property|name)=["'](?:og:description|description)["'][^>]*"""
    rb"""content=["'](.*?)["']""", re.I | re.S)
META_DESC_ALT_RE = re.compile(
    rb"""<meta[^>]+content=["'](.*?)["'][^>]*(?:property|name)=["']"""
    rb"""(?:og:description|description)["']""", re.I | re.S)

# Cookie-consent widgets emit long <p> blobs and inline JSON that would
# otherwise win the "first substantial paragraph" race.
JUNK_PARAGRAPH_RE = re.compile(
    r"cky_|pliki cookie|zaakceptowa|cookie|consent|\":\"", re.I)


def parse_pub_ts(raw):
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw.strip()).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def _jsonld_description(body):
    node = netutil.jsonld_article(body)
    if node:
        for field in ("description", "articleBody"):
            if node.get(field):
                return str(node[field])
    return ""


def _first_paragraph(body):
    for match in re.finditer(rb"<p[^>]*>(.*?)</p>", body, re.S):
        raw = match.group(1).decode("utf-8", "replace")
        if JUNK_PARAGRAPH_RE.search(raw):
            continue
        text = clean_text(raw, strip_boilerplate=True)
        if len(text) >= 80:
            return text
    return ""


def fetch_page_summary(url):
    """Best-effort summary for feeds that publish none.

    zycieczestochowy.pl — the busiest source — puts only a boilerplate footer
    in <description> and has no meta description either, so without this the
    story picker would see nothing but headlines. Tries, in order: a meta
    description, a schema.org article node, then the first real paragraph.
    """
    try:
        _, body, _, _ = http_get(url)
    except RuntimeError:
        return ""
    head = body[:400_000]
    match = META_DESC_RE.search(head) or META_DESC_ALT_RE.search(head)
    if match:
        text = clean_text(match.group(1).decode("utf-8", "replace"), strip_boilerplate=True)
        if text:
            return text
    return (clean_text(_jsonld_description(head), strip_boilerplate=True)
            or _first_paragraph(head))


def parse_feed(body):
    """Parse RSS, tolerating leading whitespace/BOM before the declaration
    (zycieczestochowy.pl serves a leading newline, which strict XML rejects)."""
    start = body.find(b"<?xml")
    if start == -1:
        start = body.find(b"<")
    if start > 0:
        body = body[start:]
    return ET.fromstring(body)


def extract_items(root, feed):
    for node in root.findall(".//item"):
        link = (node.findtext("link") or "").strip()
        guid = (node.findtext("guid") or "").strip()
        title = clean_text(node.findtext("title"))
        if not title or not (link or guid):
            continue

        key = normalise_link(link) or f"{feed['id']}:{guid}"
        summary = (clean_text(node.findtext("description"), strip_boilerplate=True)
                   or clean_text(node.findtext(CONTENT + "encoded"), strip_boilerplate=True))

        yield {
            "key": key,
            "feed_id": feed["id"],
            "title": title,
            # The de-tracked URL is what gets stored, shown and pasted into
            # Telegram — nobody wants utm_campaign in a public post.
            "link": normalise_link(link) or guid,
            "summary": summary,
            "author": clean_text(node.findtext(DC + "creator")),
            "categories": [c.text.strip() for c in node.findall("category") if (c.text or "").strip()],
            "pub_ts": parse_pub_ts(node.findtext("pubDate") or node.findtext(DC + "date")),
            "title_hash": title_hash(title),
        }


def enrich_missing_summaries(conn):
    """Fill in summaries for feeds that publish none of their own."""
    pending = store.items_missing_summary(conn)
    filled = 0
    for key, link in pending:
        summary = fetch_page_summary(link)
        if summary:
            conn.execute("UPDATE items SET summary=? WHERE key=?", (summary, key))
            filled += 1
        conn.commit()
        time.sleep(1)  # be polite to the article pages
    return filled


def run():
    conn = store.connect()
    # Day one would otherwise dump weeks of archive into the first digest.
    bootstrap = store.is_empty(conn)
    if bootstrap:
        log("empty store — marking this run as bootstrap (excluded from digests)")

    total_new = 0
    for feed in FEEDS:
        if feed.get("kind") == "scrape":
            try:
                new, seen = scrape_naszemiasto.run(conn, feed, bootstrap)
            except Exception as e:
                log(f"{feed['id']}: SCRAPE FAILED — {type(e).__name__}: {e}")
                store.save_feed_state(conn, feed["id"], error=str(e))
                conn.commit()
                continue
            store.save_feed_state(conn, feed["id"])
            conn.commit()
            total_new += new
            log(f"{feed['id']}: {seen} candidate(s) checked, {new} new")
            continue

        state = store.get_feed_state(conn, feed["id"])
        try:
            status, body, etag, last_modified = http_get(
                feed["url"], state.get("etag"), state.get("last_modified"))
        except RuntimeError as e:
            log(f"{feed['id']}: FAILED — {e}")
            store.save_feed_state(conn, feed["id"], error=str(e))
            conn.commit()
            continue

        if status == 304:
            log(f"{feed['id']}: 304 not modified")
            store.save_feed_state(conn, feed["id"])
            conn.commit()
            continue

        try:
            root = parse_feed(body)
        except ET.ParseError as e:
            log(f"{feed['id']}: PARSE FAILED — {e}")
            store.save_feed_state(conn, feed["id"], error=f"parse: {e}")
            conn.commit()
            continue

        seen = new = 0
        for item in extract_items(root, feed):
            seen += 1
            if not store.upsert_item(conn, item, bootstrap=bootstrap):
                continue
            new += 1
        store.save_feed_state(conn, feed["id"], etag=etag, last_modified=last_modified)
        conn.commit()
        total_new += new
        log(f"{feed['id']}: {seen} items, {new} new")

    enriched = enrich_missing_summaries(conn)
    log(f"done — {total_new} new item(s)"
        + (f", {enriched} summar(ies) pulled from article pages" if enriched else ""))
    conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as e:  # never let launchd see a crash loop
        log(f"FATAL: {type(e).__name__}: {e}")
        sys.exit(1)
