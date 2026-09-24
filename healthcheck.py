#!/usr/bin/env python3
"""Make sure the three rssnews launchd jobs are actually registered.

Discovered need: after a Mac reboot on 2026-09-21, launchd's per-login scan
of ~/Library/LaunchAgents silently failed to reload com.user.rssnews.fetch
and .digest — the plists and symlinks stayed perfectly valid on disk, and
launchd's disabled-services database had no record of them either; they just
weren't loaded. Nothing surfaced this for 3 days. This script is the fix:
run periodically, it re-bootstraps anything missing and tells the user via
a macOS notification only when it actually had to do that (silent otherwise,
so it doesn't become noise).

This does not explain *why* launchd skipped the reload — that's an OS-level
behavior with no further evidence to go on. It just makes sure the gap can
never again go unnoticed for more than a few hours.
"""

import subprocess
import sys

from netutil import log

ROOT = "/Users/user/Documents/Pets/rss-news"
JOBS = ["com.user.rssnews.fetch", "com.user.rssnews.digest", "com.user.rssnews.fetchrss-keepalive"]


def is_loaded(label):
    result = subprocess.run(
        ["launchctl", "print", f"gui/{subprocess.os.getuid()}/{label}"],
        capture_output=True, text=True)
    return result.returncode == 0


def bootstrap(label):
    plist = f"{ROOT}/launchd/{label}.plist"
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{subprocess.os.getuid()}", plist],
        capture_output=True, text=True)
    return result.returncode == 0, result.stderr.strip()


def notify(recovered):
    names = ", ".join(j.replace("com.user.rssnews.", "") for j in recovered)
    script = (
        f'display notification "Перезапущено: {names}" '
        f'with title "RSS-дайджест: відновлено після збою" sound name "Basso"'
    )
    subprocess.run(["osascript", "-e", script])


def run():
    recovered = []
    for label in JOBS:
        if is_loaded(label):
            continue
        ok, err = bootstrap(label)
        if ok:
            log(f"healthcheck: {label} was NOT loaded — re-registered it")
            recovered.append(label)
        else:
            log(f"healthcheck: {label} was NOT loaded — re-register FAILED: {err}")

    if recovered:
        notify(recovered)
    return 0


if __name__ == "__main__":
    sys.exit(run())
