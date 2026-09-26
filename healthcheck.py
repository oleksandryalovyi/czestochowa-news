#!/usr/bin/env python3
"""Make sure the rssnews launchd jobs are actually registered AND actually
producing fresh output.

Two distinct failure modes have been observed, both invisible to a plain
"is the job loaded" check:

1. 2026-09-21: after a reboot, launchd's per-login scan of
   ~/Library/LaunchAgents silently failed to reload com.user.rssnews.fetch
   and .digest — plists/symlinks stayed valid, launchd's disabled-services
   database had no record of them, they just weren't loaded. Nothing
   surfaced this for 3 days. `is_loaded()` below catches this: not
   registered at all → re-bootstrap.

2. 2026-09-24 to 26: after being re-registered, fetch and digest WERE loaded
   and launchd WAS trying to fire them on schedule (confirmed via `runs`
   incrementing), but every attempt failed immediately with exit code 78
   (EX_CONFIG) and zero output — launchd refused to even spawn the process.
   `is_loaded()` alone reports these as healthy, since the job genuinely is
   registered. The empirically confirmed cause was specific to their
   existing log files (open across ~3 weeks, through the reboot above):
   moving each aside and letting launchd create a fresh one at the same path
   fixed it completely; a plain bootout+bootstrap of the job itself did not.
   `is_stale()` below catches this by watching each log's mtime against how
   often that job is expected to run; recovery rotates the log file aside
   (kept, not deleted, as a forensic record) and re-bootstraps.

Neither failure mode's underlying OS-level cause is fully understood. This
script exists so neither can go unnoticed for more than a couple of hours
again, not to explain launchd's internals.
"""

import os
import subprocess
import sys
import time

from netutil import log

ROOT = "/Users/user/Documents/Pets/rss-news"
JOBS = ["com.user.rssnews.fetch", "com.user.rssnews.digest", "com.user.rssnews.fetchrss-keepalive"]

# label -> (log file, max hours since last write before it's considered stuck)
# Generous margins: fetch runs hourly, digest at 07:55/18:55 (~13h max gap),
# keepalive every 3 days. healthcheck.py's own log is deliberately excluded —
# it's expected to sit empty for days whenever nothing needs recovering, so
# staleness isn't a meaningful signal for it.
STALE_AFTER_HOURS = {
    "com.user.rssnews.fetch": ("logs/fetch.log", 3),
    "com.user.rssnews.digest": ("logs/digest.log", 15),
    "com.user.rssnews.fetchrss-keepalive": ("logs/fetchrss-keepalive.log", 96),
}


def is_loaded(label):
    result = subprocess.run(
        ["launchctl", "print", f"gui/{subprocess.os.getuid()}/{label}"],
        capture_output=True, text=True)
    return result.returncode == 0


def is_stale(label):
    entry = STALE_AFTER_HOURS.get(label)
    if not entry:
        return False
    rel_path, max_hours = entry
    path = os.path.join(ROOT, rel_path)
    if not os.path.exists(path):
        return False  # missing entirely is a different, already-handled case
    age_hours = (time.time() - os.path.getmtime(path)) / 3600
    return age_hours > max_hours


def bootout(label):
    subprocess.run(
        ["launchctl", "bootout", f"gui/{subprocess.os.getuid()}/{label}"],
        capture_output=True, text=True)


def bootstrap(label):
    plist = f"{ROOT}/launchd/{label}.plist"
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{subprocess.os.getuid()}", plist],
        capture_output=True, text=True)
    return result.returncode == 0, result.stderr.strip()


def rotate_log(label):
    """Move the job's log aside (kept, not deleted) so launchd creates a
    fresh file at that path — the confirmed fix for failure mode 2 above."""
    rel_path, _ = STALE_AFTER_HOURS[label]
    path = os.path.join(ROOT, rel_path)
    if os.path.exists(path):
        stamp = time.strftime("%Y%m%dT%H%M%S")
        os.rename(path, f"{path}.stuck-{stamp}")


def kickstart(label):
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{subprocess.os.getuid()}/{label}"],
        capture_output=True, text=True)


def notify(recovered, still_stale):
    lines = []
    if recovered:
        names = ", ".join(j.replace("com.user.rssnews.", "") for j in recovered)
        lines.append(f"Перезапущено: {names}")
    if still_stale:
        names = ", ".join(j.replace("com.user.rssnews.", "") for j in still_stale)
        lines.append(f"Досі не оновлюється: {names} — перевір вручну")
    title = ("RSS-дайджест: потрібна увага" if still_stale
             else "RSS-дайджест: відновлено після збою")
    script = (
        f'display notification "{" | ".join(lines)}" '
        f'with title "{title}" sound name "Basso"'
    )
    subprocess.run(["osascript", "-e", script])


def run():
    recovered = []
    still_stale = []

    for label in JOBS:
        if not is_loaded(label):
            ok, err = bootstrap(label)
            if ok:
                log(f"healthcheck: {label} was NOT loaded — re-registered it")
                recovered.append(label)
            else:
                log(f"healthcheck: {label} was NOT loaded — re-register FAILED: {err}")
                still_stale.append(label)
            continue

        if is_stale(label):
            log(f"healthcheck: {label} is loaded but its log is stale — "
                f"rotating log and re-registering (see file history note in the script)")
            bootout(label)
            rotate_log(label)
            ok, err = bootstrap(label)
            if ok:
                kickstart(label)  # don't wait for it — could take up to ~90s
                recovered.append(label)
            else:
                log(f"healthcheck: {label} re-register after rotate FAILED: {err}")
                still_stale.append(label)

    if recovered or still_stale:
        notify(recovered, still_stale)
    return 0


if __name__ == "__main__":
    sys.exit(run())
