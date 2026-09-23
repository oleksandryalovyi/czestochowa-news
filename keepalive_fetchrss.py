#!/usr/bin/env python3
"""Touch the FetchRSS feed periodically so FetchRSS doesn't delete it for
inactivity.

This feed is otherwise dormant — fetch.py only calls it when
scrape_naszemiasto.py fails (see fetch.py's fallback path). If the scraper
just keeps working, FetchRSS never sees a request and eventually deletes the
feed, which would silently remove the safety net. This script's only job is
to make one real request every few days so that never happens; it does not
parse or store anything (the fallback path already does that when it's
actually needed).

Run every 3 days from launchd (launchd/com.user.rssnews.fetchrss-keepalive.plist).
"""

import sys

import netutil
from feeds import FEEDS_BY_ID
from netutil import log

URL = FEEDS_BY_ID["naszemiasto"]["fallback_url"]


def run():
    try:
        status, body, _, _ = netutil.http_get(URL)
    except RuntimeError as e:
        log(f"fetchrss-keepalive: FAILED — {e}")
        return 1
    log(f"fetchrss-keepalive: touched {URL} — HTTP {status}, {len(body)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(run())
