#!/usr/bin/env python3
"""Build the last-24h digest from the store and publish it.

Writes digests/YYYY-MM-DD.{json,md} plus the stable digests/latest.{json,md}
that the scheduled Claude task reads, then mirrors the digest onto one fixed
Notion page.
"""

import json
import os
import shutil
import sys
from collections import OrderedDict
from datetime import datetime, timezone

import notion
import store
from feeds import FEEDS, FEEDS_BY_ID

ROOT = os.path.dirname(os.path.abspath(__file__))
DIGESTS = os.path.join(ROOT, "digests")
WINDOW_HOURS = 24
LOCAL_TZ = datetime.now().astimezone().tzinfo


def log(msg):
    print(f"[{store.iso(store.utcnow())}] {msg}", flush=True)


def local(iso_ts):
    return datetime.fromisoformat(iso_ts).astimezone(LOCAL_TZ)


def stamp(item, today):
    """HH:MM for today's items, DD.MM HH:MM for yesterday's — a bare HH:MM
    across a 24h window reads as out-of-order."""
    when = local(item["published_at"])
    text = f"{when:%H:%M}" if when.date() == today else f"{when:%d.%m %H:%M}"
    return text + ("~" if item["published_at_is_estimate"] else "")


def group_by_source(items):
    grouped = OrderedDict((f["name"], []) for f in FEEDS)
    for item in items:
        grouped[item["source"]].append(item)
    return OrderedDict((k, v) for k, v in grouped.items() if v)


def source_status(conn):
    out = []
    for feed in FEEDS:
        state = store.get_feed_state(conn, feed["id"])
        out.append({
            "id": feed["id"],
            "name": feed["name"],
            "last_ok": state.get("last_ok"),
            "last_error": state.get("last_error"),
            "healthy": bool(state.get("last_ok")) and not state.get("last_error"),
        })
    return out


def build_markdown(payload):
    grouped = group_by_source(payload["items"])
    lines = [
        f"# Częstochowa — {payload['date']}",
        "",
        f"{payload['count']} materiał(ów) z ostatnich {WINDOW_HOURS} h "
        f"(wygenerowano {local(payload['generated_at']):%Y-%m-%d %H:%M %Z}). "
        f"`~` = czas przybliżony (kanał nie podaje wiarygodnej daty publikacji).",
        "",
    ]
    today = local(payload["generated_at"]).date()
    for source, items in grouped.items():
        lines += [f"## {source} ({len(items)})", ""]
        for item in items:
            lines.append(f"- **{stamp(item, today)}** [{item['title']}]({item['link']})")
            if item["summary"]:
                lines.append(f"  {item['summary']}")
        lines.append("")

    unhealthy = [s for s in payload["sources"] if not s["healthy"]]
    if unhealthy:
        lines += ["## ⚠️ Problemy ze źródłami", ""]
        lines += [f"- {s['name']}: {s['last_error'] or 'brak udanego pobrania'}" for s in unhealthy]
        lines.append("")
    if not payload["items"]:
        lines += ["_Brak nowych materiałów w oknie 24 h._", ""]
    return "\n".join(lines)


def build_blocks(payload):
    blocks = [notion.paragraph([notion.text(
        f"{payload['count']} materiał(ów) z ostatnich {WINDOW_HOURS} h · "
        f"aktualizacja {local(payload['generated_at']):%Y-%m-%d %H:%M}"
    )])]
    today = local(payload["generated_at"]).date()
    for source, items in group_by_source(payload["items"]).items():
        blocks.append(notion.heading(f"{source} ({len(items)})", level=2))
        for item in items:
            rich = [notion.text(f"{stamp(item, today)}  "),
                    notion.text(item["title"], link=item["link"], bold=True)]
            if item["summary"]:
                rich.append(notion.text(f" — {item['summary']}"))
            blocks.append(notion.paragraph(rich))

    unhealthy = [s for s in payload["sources"] if not s["healthy"]]
    if unhealthy:
        blocks.append(notion.divider())
        blocks.append(notion.heading("⚠️ Problemy ze źródłami", level=3))
        for s in unhealthy:
            blocks.append(notion.paragraph([notion.text(
                f"{s['name']}: {s['last_error'] or 'brak udanego pobrania'}")]))
    if not payload["items"]:
        blocks.append(notion.paragraph([notion.text("Brak nowych materiałów w oknie 24 h.")]))
    return blocks


def publish_to_notion(payload, blocks):
    env = notion.load_env()
    token, page_id = env.get("NOTION_TOKEN"), env.get("NOTION_PAGE_ID")
    if not token or not page_id:
        log("Notion: skipped (NOTION_TOKEN / NOTION_PAGE_ID not set in .env)")
        return
    try:
        removed = notion.Notion(token).replace_page(
            page_id, f"Częstochowa digest", blocks)
        log(f"Notion: page rewritten in place ({removed} old block(s) removed, "
            f"{len(blocks)} written)")
    except notion.NotionError as e:
        # The local digest is the source of truth; never fail the run over Notion.
        log(f"Notion: FAILED — {e}")


def run():
    os.makedirs(DIGESTS, exist_ok=True)
    conn = store.connect()
    items = store.recent_items(conn, FEEDS_BY_ID, hours=WINDOW_HOURS)
    now = store.utcnow()
    payload = {
        "date": now.astimezone(LOCAL_TZ).strftime("%Y-%m-%d"),
        "generated_at": store.iso(now),
        "window_hours": WINDOW_HOURS,
        "count": len(items),
        "sources": source_status(conn),
        "items": items,
    }
    conn.close()

    # Build the Notion payload before touching anything remote.
    blocks = build_blocks(payload)
    markdown = build_markdown(payload)

    filepath = os.path.join(DIGESTS, "latest")
    with open(f"{filepath}.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    with open(f"{filepath}.md", "w", encoding="utf-8") as fh:
        fh.write(markdown)

    log(f"digest: {len(items)} item(s) → latest.md")

    publish_to_notion(payload, blocks)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as e:
        log(f"FATAL: {type(e).__name__}: {e}")
        sys.exit(1)
