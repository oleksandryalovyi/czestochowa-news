"""Minimal Notion REST client (stdlib only).

Only what the digest needs: rewrite one fixed page's content in place. The
page is never re-created — its id is a constant in .env, so the URL you
bookmark stays valid forever.
"""

import json
import os
import urllib.error
import urllib.request

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
APPEND_CHUNK = 100  # Notion rejects more than 100 children per call.


class NotionError(RuntimeError):
    pass


def load_env(path=None):
    """Read a .env file without adding a dependency. Real env vars win."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    values = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip("'\"")
    for k in ("NOTION_TOKEN", "NOTION_PAGE_ID"):
        if os.environ.get(k):
            values[k] = os.environ[k]
    return values


class Notion:
    def __init__(self, token):
        self.token = token

    def _request(self, method, path, payload=None):
        req = urllib.request.Request(
            f"{API}{path}",
            method=method,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": VERSION,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise NotionError(f"{method} {path} → HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise NotionError(f"{method} {path} → {e}") from None

    def list_children(self, block_id):
        out, cursor = [], None
        while True:
            path = f"/blocks/{block_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            data = self._request("GET", path)
            out.extend(data.get("results", []))
            if not data.get("has_more"):
                return out
            cursor = data.get("next_cursor")

    def delete_block(self, block_id):
        self._request("DELETE", f"/blocks/{block_id}")

    def append_children(self, block_id, blocks):
        for i in range(0, len(blocks), APPEND_CHUNK):
            self._request("PATCH", f"/blocks/{block_id}/children",
                          {"children": blocks[i:i + APPEND_CHUNK]})

    def set_title(self, page_id, title):
        self._request("PATCH", f"/pages/{page_id}",
                      {"properties": {"title": {"title": [text(title)]}}})

    def replace_page(self, page_id, title, blocks):
        """Rewrite the page in place.

        Callers must build `blocks` before calling: the delete happens first
        and is not atomic, so a payload built afterwards could fail and leave
        the page empty.
        """
        existing = self.list_children(page_id)
        for block in existing:
            self.delete_block(block["id"])
        self.append_children(page_id, blocks)
        self.set_title(page_id, title)
        return len(existing)


# --- block helpers -----------------------------------------------------------

def text(content, link=None, bold=False):
    node = {"type": "text", "text": {"content": content[:2000]}}
    if link:
        node["text"]["link"] = {"url": link}
    if bold:
        node["annotations"] = {"bold": True}
    return node


def heading(content, level=2):
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": [text(content)]}}


def paragraph(rich):
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich}}


def divider():
    return {"object": "block", "type": "divider", "divider": {}}
