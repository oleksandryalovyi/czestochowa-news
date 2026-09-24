# Częstochowa RSS → daily digest → Telegram draft

Collects four local Częstochowa news feeds, keeps a rolling record of everything
published, and every morning produces a 24-hour digest that a scheduled Claude
task reads to draft one Ukrainian Telegram post.

## Why there is a database and not just a daily fetch

The sources cannot be asked "what was published in the last 24 hours":

| Source | Problem |
|---|---|
| `zycieczestochowy.pl` | Feed only holds ~11 hours of items — a once-a-day fetch silently loses stories. Also serves a leading newline before `<?xml`, and publishes **no summary** (only a boilerplate footer). |
| `czestochowa.naszemiasto.pl` | No real per-city RSS feed at all — see below. |
| `informacje-czest.pl` | Returns HTTP 429 to any request without a browser `User-Agent`. |
| `gazetacz.com.pl` | Fine, but appends a boilerplate footer to every summary. |

So `fetch.py` runs **hourly**, records `first_seen` on insert, and the digest
window is computed from observed data. Where a source reports no trustworthy
`pubDate` (`trust_pubdate: false` in `feeds.py`), `first_seen` is used instead
and the digest marks that time with `~`.

### naszemiasto.pl: scraped, not RSS

This source used to go through a third-party FetchRSS-generated feed, which
stamped every item with its own scrape time (not the article's real publish
time) and only ever produced ~5 items. It's now scraped directly
(`scrape_naszemiasto.py`).

The site's homepage is a network hub: genuine Częstochowa articles sit in the
same tiles as syndicated national content (car news, travel, business, sport
from other cities), with no visible difference in the listing HTML. The only
reliable signal found is each article's own embedded JSON-LD
`articleSection` — `"Wiadomosci"` is local; everything else observed
(`Strefa-biznesu`, `Telemagazyn`, `Kulinaria`, `Turystyka`, `Sport`,
`Strefa-agro`, `Rozrywka`, `Pogoda`, `Wydarzenia`, ...) is syndicated/generic.
That filter isn't perfect — some regional/national wire pieces get bucketed
under "Wiadomosci" too — so genuine local-relevance judgment still happens at
the Telegram-pick step, same as for the other three sources.

Since classifying a link means opening the article page, `scrape_classification`
in the database caches that decision forever per URL (a section doesn't change
after publication) — otherwise every article still listed on the homepage
would get re-fetched every hour for no reason. A fresh backlog is capped at
40 new classifications per run (`MAX_NEW_CLASSIFICATIONS_PER_RUN` in
`scrape_naszemiasto.py`) and picked up over subsequent hourly runs, so it never
turns one poll into a multi-minute crawl.

**Fallback:** if the scraper errors for any reason (most likely a 403 if the
site starts blocking it), `fetch.py` falls back for that run to the old
FetchRSS-generated feed (`feeds.py`'s `fallback_url`) rather than losing the
source entirely. Fallback items never get their `pubDate` trusted — FetchRSS
stamps its own scrape time, not the real publish time — so they fall back to
`first_seen`, same as before the scraper existed. A fallback that succeeds is
still logged clearly in `logs/fetch.log`, but is *not* surfaced as an
unhealthy source in the digest, since the source did come through — just via
the safety net. Only when both the scrape and the fallback fail is it marked
unhealthy.

**Keeping the fallback alive:** FetchRSS deletes feeds it hasn't been asked
for in a while. Since the fallback is only ever hit when the scraper fails,
it could otherwise sit untouched for months and get deleted right when it's
actually needed. `keepalive_fetchrss.py` exists purely to prevent that — it
makes one plain request to the FetchRSS URL every 3 days
(`com.user.rssnews.fetchrss-keepalive`, see below) and does nothing else: no
parsing, no storing. The real fallback logic lives only in `fetch.py`.

**Note on robots.txt:** `czestochowa.naszemiasto.pl/robots.txt` names
`ClaudeBot` (along with `GPTBot`, `CCBot`, etc.) as disallowed, while leaving
`User-agent: *` open for general browsing. This was flagged to and confirmed
by the user before writing the scraper, on the basis that this is a single
low-volume personal fetch (one homepage request/hour) rather than the
large-scale AI-training crawling that directive targets.

## Pieces

| File | Role |
|---|---|
| `feeds.py` | Source list — RSS feeds and the naszemiasto scraper, plus the `trust_pubdate` flag |
| `netutil.py` | Shared HTTP + text helpers (User-Agent, retries, link/text cleanup, JSON-LD parsing) used by both `fetch.py` and `scrape_naszemiasto.py` |
| `fetch.py` | Hourly poll → SQLite. Conditional GET, retries, per-source failure isolation, page-summary enrichment, dispatches to the scraper for `kind: "scrape"` sources |
| `scrape_naszemiasto.py` | naszemiasto.pl scraper — see above |
| `keepalive_fetchrss.py` | Pings the FetchRSS fallback feed every 3 days so FetchRSS doesn't delete it for inactivity — see above |
| `healthcheck.py` | Every 2h, re-registers any of the launchd jobs below that silently isn't loaded, and notifies if it had to — see "If a launchd job goes missing" below |
| `store.py` | Schema, `first_seen` bookkeeping, the scrape classification cache, the 24h window query |
| `digest.py` | Builds `digests/YYYY-MM-DD.{json,md}` + `latest.{json,md}`, mirrors to Notion |
| `notion.py` | Minimal Notion REST client |
| `launchd/` | The two LaunchAgents |

Stdlib only — no venv, nothing for `launchd` to activate.

## Schedule

- `com.user.rssnews.fetch` — hourly at **:17**
- `com.user.rssnews.digest` — daily at **07:55** and **18:55**
- `com.user.rssnews.fetchrss-keepalive` — every **3 days** (`StartInterval`,
  not a fixed clock time — see below), plus once immediately whenever the job
  is loaded
- `com.user.rssnews.healthcheck` — every **2 hours**, plus once immediately on
  load — see "If a launchd job goes missing" below
- Claude scheduled task `czestochowa-telegram-pick` — daily around **08:05**,
  reads `digests/latest.json` and writes `drafts/telegram.md`, appends it to a
  Notion page, and fires a macOS notification when done (see its `SKILL.md`
  in `~/.claude/scheduled-tasks/czestochowa-telegram-pick/`)

The launchd jobs run whether or not the Claude app is open. The Claude task
only fires while the app is running (a missed run happens on next launch).

```bash
launchctl list | grep rssnews                       # status
launchctl kickstart -k gui/$(id -u)/com.user.rssnews.fetch   # run now
tail -f logs/fetch.log
```

## If a launchd job goes missing

Normally `~/Library/LaunchAgents` auto-reloads on every login/reboot — no
manual step should ever be needed. On 2026-09-21, though, a reboot left
`com.user.rssnews.fetch` and `.digest` unregistered (not disabled, not
crashed — `launchctl print` just reported "Could not find service"), and
nothing surfaced that for 3 days.

`healthcheck.py` exists to catch a repeat: every 2 hours it checks whether
`fetch`, `digest`, and `fetchrss-keepalive` are actually loaded and, if not,
re-runs `launchctl bootstrap` on them itself. It only sends a macOS
notification when it actually had to fix something — silent otherwise. Worst
case after a bad reboot, you're back up within ~2 hours with a notification,
not silence for days.

The healthcheck job is itself a LaunchAgent, so it shares the same small,
unexplained exposure the other two had. To check everything by hand at any
time:

```bash
launchctl list | grep rssnews   # should show all four jobs
```

If one's ever missing, reload it directly:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.rssnews.<name>.plist
```

## Notion

Optional — the local digest is the source of truth and a Notion failure never
fails a run. Copy `.env.example` to `.env` and follow the steps in it. The
digest always rewrites **one fixed page** in place; daily history stays in
`digests/`.

## Manual use

```bash
python3 fetch.py    # poll now
python3 digest.py   # rebuild digest from what is already stored
```
