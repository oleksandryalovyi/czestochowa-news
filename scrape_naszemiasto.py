"""Scraper for czestochowa.naszemiasto.pl — replaces the FetchRSS-generated
feed for this source.

The site has no real per-city RSS feed. Its homepage is a network hub: genuine
Częstochowa articles are mixed in with syndicated national content (car news,
travel, business, sport from other cities) served from the same domain and
listed in the same tiles, with no distinguishing markup in the listing HTML
itself.

The only reliable signal found is each article's own embedded JSON-LD
(`articleSection`): local news is tagged "Wiadomosci"; everything syndicated
carries a different vertical name (Strefa-biznesu, Telemagazyn, Kulinaria,
Turystyka, Sport, Strefa-agro, Wydarzenia, ...). So every candidate link has to
be opened once to find out which it is. That JSON-LD also hands us a real
`datePublished` and a real `description` — better than the RSS feed this
replaces, which stamped every item with its own scrape time.

To avoid re-opening the same article every hour just because it is still
sitting in the homepage listing, classifications are cached in
`scrape_classification` (see store.py) keyed by URL, forever (a section
doesn't change after publication).

Note (deliberate, not an oversight): czestochowa.naszemiasto.pl/robots.txt
disallows the named crawler "ClaudeBot" (along with GPTBot, CCBot, etc.)
while leaving `User-agent: *` open for general browsing. This scraper uses
the same generic browser User-Agent as the rest of the pipeline. The user
was shown that directive and asked to confirm before this file was written;
they chose to proceed, on the basis that this is a single, low-volume,
personal fetch (one homepage request/hour) rather than the large-scale
AI-training crawling the opt-out targets.
"""

import re
import time
from datetime import datetime, timezone

import netutil
import store
from netutil import clean_text, http_get, log, normalise_link, title_hash

HOMEPAGE = "https://czestochowa.naszemiasto.pl/"

# Only this section is genuinely local Częstochowa news; every other value
# observed (Strefa-biznesu, Telemagazyn, Kulinaria, Turystyka, Sport,
# Strefa-agro, Wydarzenia, ...) was syndicated/national/evergreen filler.
ACCEPTED_SECTIONS = {"Wiadomosci"}

# /some-slug/ar/c<section>[p<n>]-<article-id> — relative links only, which
# excludes the outright cross-domain syndication (motofakty.pl etc.); the
# articleSection check below then excludes same-domain syndication too.
ARTICLE_LINK_RE = re.compile(r'href="(/[a-z0-9-]+/ar/c\d+p?\d*-\d+)"')

MAX_NEW_CLASSIFICATIONS_PER_RUN = 40  # politeness cap; steady-state need is far lower
CLASSIFY_DELAY = 1  # seconds between article-page fetches


def _candidate_links(html_body):
    seen, links = set(), []
    for path in ARTICLE_LINK_RE.findall(html_body.decode("utf-8", "replace")):
        url = "https://czestochowa.naszemiasto.pl" + path
        key = normalise_link(url)
        if key not in seen:
            seen.add(key)
            links.append(url)
    return links


def _parse_published(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _classify_and_build(url):
    """Fetch one article page. Returns (item_dict_or_None, section_or_None)."""
    try:
        _, body, _, _ = http_get(url)
    except RuntimeError as e:
        log(f"naszemiasto: fetch failed for {url} — {e}")
        return None, None

    node = netutil.jsonld_article(body)
    if not node:
        return None, None

    section = node.get("articleSection")
    if section not in ACCEPTED_SECTIONS:
        return None, section

    title = clean_text(node.get("headline") or "")
    canonical = normalise_link(node.get("url") or url) or normalise_link(url)
    if not title or not canonical:
        return None, section

    item = {
        "key": canonical,
        "feed_id": "naszemiasto",
        "title": title,
        "link": canonical,
        "summary": clean_text(node.get("description") or ""),
        "author": clean_text(((node.get("author") or {}).get("name")) or ""),
        "categories": [section] if section else [],
        "pub_ts": _parse_published(node.get("datePublished")),
        "title_hash": title_hash(title),
    }
    return item, section


def run(conn, feed, bootstrap):
    """Poll the homepage, classify new links, upsert accepted ones.

    Returns (new_items, candidates_seen), matching the logging shape fetch.py
    uses for RSS sources.
    """
    _, body, _, _ = http_get(HOMEPAGE)
    candidates = _candidate_links(body)

    new_items = 0
    classified_this_run = 0
    for url in candidates:
        key = normalise_link(url)
        cached = store.get_classification(conn, key)
        if cached is not None:
            # Already classified — a section doesn't change after publication,
            # and re-fetching every still-listed article every hour just to
            # catch a rare edit is exactly the cost this cache exists to avoid.
            continue
        if classified_this_run >= MAX_NEW_CLASSIFICATIONS_PER_RUN:
            continue  # picked up on a later run

        item, section = _classify_and_build(url)
        store.save_classification(conn, key, section, accepted=item is not None)
        conn.commit()
        classified_this_run += 1
        time.sleep(CLASSIFY_DELAY)

        if item is not None and store.upsert_item(conn, item, bootstrap=bootstrap):
            new_items += 1

    return new_items, len(candidates)
