"""SQLite store of every article seen across the feeds.

The store exists because the feeds cannot be trusted to answer "what was
published in the last 24 hours" on their own: the busiest one only keeps ~11
hours of items, and one of them reports its own scrape time as pubDate. By
polling hourly and recording first_seen on insert, the 24h window is computed
from data we observed rather than from what a feed claims today.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "items.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  key        TEXT PRIMARY KEY,
  feed_id    TEXT NOT NULL,
  title      TEXT NOT NULL,
  link       TEXT NOT NULL,
  summary    TEXT,
  author     TEXT,
  categories TEXT,
  pub_ts     TEXT,
  first_seen TEXT NOT NULL,
  title_hash TEXT,
  bootstrap  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_first_seen ON items(first_seen);
CREATE INDEX IF NOT EXISTS items_title_hash ON items(title_hash);

CREATE TABLE IF NOT EXISTS feed_state (
  feed_id       TEXT PRIMARY KEY,
  etag          TEXT,
  last_modified TEXT,
  last_ok       TEXT,
  last_error    TEXT
);

-- Scrapers (as opposed to RSS feeds) have to open each article page to find
-- out whether it's genuinely local content or syndicated network filler.
-- Without this cache, the same article sitting on a homepage listing would
-- get re-fetched and re-classified every single hourly poll.
CREATE TABLE IF NOT EXISTS scrape_classification (
  link       TEXT PRIMARY KEY,
  section    TEXT,
  accepted   INTEGER NOT NULL,
  checked_at TEXT NOT NULL
);
"""


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def is_empty(conn):
    return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def upsert_item(conn, item, bootstrap=False):
    """Insert if new. Returns True when the item was not seen before.

    Existing rows keep their original first_seen — that timestamp is the whole
    point of the store — but refresh mutable fields, since sites do edit
    headlines and summaries after publishing.
    """
    now = iso(utcnow())
    cur = conn.execute(
        """INSERT INTO items
             (key, feed_id, title, link, summary, author, categories,
              pub_ts, first_seen, title_hash, bootstrap)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET
             title=excluded.title,
             -- Never clobber a good summary with an empty one: the feeds that
             -- need page enrichment publish an empty description forever, so a
             -- plain assignment here wipes the enrichment on every poll.
             summary=COALESCE(NULLIF(excluded.summary, ''), items.summary),
             categories=excluded.categories,
             pub_ts=COALESCE(excluded.pub_ts, items.pub_ts),
             title_hash=excluded.title_hash""",
        (
            item["key"], item["feed_id"], item["title"], item["link"],
            item.get("summary"), item.get("author"),
            json.dumps(item.get("categories") or [], ensure_ascii=False),
            item.get("pub_ts"), now, item.get("title_hash"),
            1 if bootstrap else 0,
        ),
    )
    return cur.rowcount == 1 and conn.execute(
        "SELECT first_seen FROM items WHERE key=?", (item["key"],)
    ).fetchone()["first_seen"] == now


def get_classification(conn, link):
    row = conn.execute(
        "SELECT * FROM scrape_classification WHERE link=?", (link,)).fetchone()
    return dict(row) if row else None


def save_classification(conn, link, section, accepted):
    conn.execute(
        """INSERT INTO scrape_classification (link, section, accepted, checked_at)
           VALUES (?,?,?,?)
           ON CONFLICT(link) DO UPDATE SET
             section=excluded.section, accepted=excluded.accepted,
             checked_at=excluded.checked_at""",
        (link, section, 1 if accepted else 0, iso(utcnow())))


def items_missing_summary(conn, hours=48, limit=25):
    """Recent items still without a summary — enrichment retry queue.

    Covers both the first attempt failing and items stored before enrichment
    existed. Bounded so one bad day cannot turn an hourly poll into a crawl.
    """
    cutoff = iso(utcnow() - timedelta(hours=hours))
    rows = conn.execute(
        """SELECT key, link FROM items
            WHERE (summary IS NULL OR summary = '')
              AND first_seen >= ?
              AND link LIKE 'http%'
            ORDER BY first_seen DESC LIMIT ?""",
        (cutoff, limit)).fetchall()
    return [(r["key"], r["link"]) for r in rows]


def save_feed_state(conn, feed_id, etag=None, last_modified=None, error=None):
    conn.execute(
        """INSERT INTO feed_state (feed_id, etag, last_modified, last_ok, last_error)
           VALUES (?,?,?,?,?)
           ON CONFLICT(feed_id) DO UPDATE SET
             etag=COALESCE(excluded.etag, feed_state.etag),
             last_modified=COALESCE(excluded.last_modified, feed_state.last_modified),
             last_ok=COALESCE(excluded.last_ok, feed_state.last_ok),
             last_error=excluded.last_error""",
        (feed_id, etag, last_modified, None if error else iso(utcnow()), error),
    )


def get_feed_state(conn, feed_id):
    row = conn.execute("SELECT * FROM feed_state WHERE feed_id=?", (feed_id,)).fetchone()
    return dict(row) if row else {}


def effective_ts(row, trust_pubdate):
    """The timestamp the 24h filter runs on.

    pub_ts when the feed reports a real publish time, otherwise the moment we
    first observed the item.
    """
    if trust_pubdate and row["pub_ts"]:
        return row["pub_ts"]
    return row["first_seen"]


def recent_items(conn, feeds_by_id, hours=24):
    """Items inside the window, newest first, deduplicated across feeds."""
    cutoff = utcnow() - timedelta(hours=hours)
    out = []
    for row in conn.execute("SELECT * FROM items"):
        feed = feeds_by_id.get(row["feed_id"])
        if not feed:
            continue
        is_estimate = not (feed["trust_pubdate"] and row["pub_ts"])
        # Items whose age we can only estimate look brand new on the very first
        # poll, so a bootstrap run would flood day one with unknown-age archive.
        # Items with a real pubDate are safe — the 24h filter already dates them.
        if row["bootstrap"] and is_estimate:
            continue
        ts = effective_ts(row, feed["trust_pubdate"])
        try:
            when = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            continue
        if when < cutoff:
            continue
        out.append({
            "id": row["key"],
            "source": feed["name"],
            "source_id": feed["id"],
            "title": row["title"],
            "link": row["link"],
            "summary": row["summary"] or "",
            "author": row["author"] or "",
            "categories": json.loads(row["categories"] or "[]"),
            "published_at": when.astimezone(timezone.utc).isoformat(),
            "published_at_is_estimate": is_estimate,
            "_title_hash": row["title_hash"],
        })

    out.sort(key=lambda i: i["published_at"], reverse=True)

    # The same story can appear on two sites; keep the earliest-published copy.
    deduped, seen = [], set()
    for item in sorted(out, key=lambda i: i["published_at"]):
        h = item.pop("_title_hash")
        if h and h in seen:
            continue
        if h:
            seen.add(h)
        deduped.append(item)
    deduped.sort(key=lambda i: i["published_at"], reverse=True)
    return deduped
