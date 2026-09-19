"""Shared HTTP + text helpers used by both the RSS fetcher (fetch.py) and the
naszemiasto scraper (scrape_naszemiasto.py) — one User-Agent, one retry
policy, one link/text normalisation, so both sources behave identically
towards the sites they hit and dedupe against each other correctly.
"""

import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import store

# informacje-czest.pl answers 429 to anything that looks like a bot; a
# generic browser UA is also simply the polite default for everything else.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = 30
RETRIES = 2
SUMMARY_MAX = 400

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|ref$)")
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

# Several WordPress feeds append a "Artykuł X pochodzi z serwisu Y." footer to
# every description; on zycieczestochowy.pl it is the *entire* description,
# leaving no real summary behind.
BOILERPLATE_RE = re.compile(r"\s*Artykuł\s.*?\spochodzi z serwisu\s.*?\.\s*$", re.S)

ARTICLE_TYPES = {"Article", "NewsArticle", "BlogPosting", "ReportageNewsArticle"}


def log(msg):
    print(f"[{store.iso(store.utcnow())}] {msg}", flush=True)


def normalise_link(url):
    """Strip tracking params and trailing slash so the same article from two
    referrers collapses to one key."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path,
                       urlencode(query), ""))


def clean_text(raw, strip_boilerplate=False):
    if not raw:
        return ""
    text = html.unescape(TAG_RE.sub(" ", html.unescape(raw)))
    if strip_boilerplate:
        text = BOILERPLATE_RE.sub("", text)
    text = WS_RE.sub(" ", text).strip()
    return text[:SUMMARY_MAX].rstrip() + "…" if len(text) > SUMMARY_MAX else text


def title_hash(title):
    """Loose fingerprint for cross-feed duplicate detection."""
    norm = WS_RE.sub(" ", re.sub(r"[^\w\s]", "", (title or "").lower(), flags=re.UNICODE)).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest() if norm else None


def http_get(url, etag=None, last_modified=None):
    """Conditional GET with retries. Returns (status, body, etag, last_modified)."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/rss+xml,application/xml,text/xml,*/*",
        "Accept-Language": "pl,en;q=0.8",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return (resp.status, resp.read(),
                        resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, b"", etag, last_modified
            last_err = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = str(e)
        if attempt < RETRIES:
            time.sleep(2 ** attempt * 3)
    raise RuntimeError(last_err or "unknown fetch error")


def jsonld_nodes(body):
    """Yield every dict found in the page's JSON-LD <script> blocks, with
    @graph arrays flattened."""
    for match in re.finditer(
            rb"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", body, re.S):
        try:
            data = json.loads(match.group(1).decode("utf-8", "replace"))
        except ValueError:
            continue
        nodes = data.get("@graph", data) if isinstance(data, dict) else data
        for node in (nodes if isinstance(nodes, list) else [nodes]):
            if isinstance(node, dict):
                yield node


def jsonld_article(body):
    """The first Article/NewsArticle/BlogPosting node on the page, if any."""
    for node in jsonld_nodes(body):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(t in ARTICLE_TYPES for t in types):
            return node
    return None
