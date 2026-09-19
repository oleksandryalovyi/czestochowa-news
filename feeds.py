"""Feed configuration for the Częstochowa news digest."""

FEEDS = [
    {
        "id": "naszemiasto",
        "name": "Częstochowa Nasze Miasto",
        # Self-built scraper (scrape_naszemiasto.py) replacing the old FetchRSS
        # feed. The site has no real per-city RSS; the homepage mixes local
        # articles with syndicated national content, separated only by each
        # article's own JSON-LD articleSection ("Wiadomosci" = local). This
        # gives a real datePublished, unlike FetchRSS which stamped every item
        # with its own scrape time.
        "kind": "scrape",
        "trust_pubdate": True,
    },
    {
        "id": "informacje-czest",
        "name": "Informacje Częstochowskie",
        "url": "https://informacje-czest.pl/rss",
        # Returns HTTP 429 without a browser User-Agent; see fetch.USER_AGENT.
        "trust_pubdate": True,
    },
    {
        "id": "zycieczestochowy",
        "name": "Życie Częstochowy",
        "url": "https://zycieczestochowy.pl/rss",
        # Busiest source; its feed only covers ~11h, hence the hourly poll.
        # Also serves a leading newline before <?xml — handled in fetch.py.
        "trust_pubdate": True,
    },
    {
        "id": "gazetacz",
        "name": "Gazeta Częstochowska",
        "url": "https://gazetacz.com.pl/rss",
        # Real per-article pubDate.
        "trust_pubdate": True,
    }
]

FEEDS_BY_ID = {f["id"]: f for f in FEEDS}
