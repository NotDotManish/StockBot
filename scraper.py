"""
scraper.py
==========
Multi-source data ingestion engine for StockBot v2.

Provides five fetchers that each return a list of standardised Article objects:

    ┌─────────────────┬──────────────────────────────────────────────┐
    │ Class           │ Source                                       │
    ├─────────────────┼──────────────────────────────────────────────┤
    │ RSSFetcher      │ Yahoo Finance, CNBC Tech, MarketWatch,       │
    │                 │ Reuters, Seeking Alpha — via feedparser      │
    │ RedditFetcher   │ r/stocks, r/investing, r/wsb — via RSS       │
    │                 │ (includes HTML-stripping of descriptions)    │
    │ SECFetcher      │ EDGAR 8-K and Form 4 filings — via RSS       │
    │ TwitterFetcher  │ @DeItaone, @unusual_whales, etc.             │
    │                 │ via tweety-ns (guest-mode, no API key)       │
    │ Scraper         │ Orchestrates all four; deduplicates & sorts  │
    └─────────────────┴──────────────────────────────────────────────┘

Article model
-------------
Each Article has a `source_type` field ("rss" | "reddit" | "sec" | "twitter")
used downstream by the alerts module to pick the right emoji and formatter.

Error handling
--------------
Every fetcher wraps its network calls in try/except so a single source
going down (rate-limited, offline, changed URL) never stops the others.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

import feedparser

from config import Config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared Article model
# ---------------------------------------------------------------------------

@dataclass
class Article:
    """
    Normalised representation of a news item from any source.

    source_type values
    ------------------
    "rss"     — traditional news feed (Yahoo Finance, CNBC, MarketWatch …)
    "reddit"  — Reddit post via RSS
    "sec"     — SEC EDGAR filing
    "twitter" — X / Twitter post via tweety-ns
    """

    id: str              # SHA-256 deduplication key
    headline: str        # Title / tweet text
    summary: str         # Body snippet (cleaned)
    source: str          # Human-readable source name (e.g. "Yahoo Finance")
    source_type: str     # "rss" | "reddit" | "sec" | "twitter"
    url: str             # Direct link to the item
    published: datetime  # Timezone-aware UTC datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "headline": self.headline,
            "summary": self.summary,
            "source": self.source,
            "source_type": self.source_type,
            "url": self.url,
            "published": self.published.isoformat(),
        }


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def _make_id(headline: str, source: str) -> str:
    """Stable 16-char deduplication key: SHA-256 of lowercase headline+source."""
    raw = f"{headline.lower().strip()}|{source.lower().strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _parse_datetime(value: Any) -> datetime:
    """
    Convert a feedparser time struct or ISO string to a UTC-aware datetime.
    Falls back to now() on any parse failure.
    """
    if value is None:
        return datetime.now(timezone.utc)

    # feedparser returns a time.struct_time (9-tuple)
    if hasattr(value, "tm_year"):
        import calendar
        try:
            ts = calendar.timegm(value)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            pass

    # ISO string
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass

    return datetime.now(timezone.utc)


def _clean_html(raw: str) -> str:
    """
    Strip HTML tags and decode HTML entities from a string.

    Uses only stdlib — no extra dependencies.
    Example: '<p>Apple &amp; Tesla</p>' → 'Apple & Tesla'
    """
    if not raw:
        return ""
    # Unescape entities first (&amp; → &, &lt; → <, etc.)
    unescaped = html.unescape(raw)
    # Strip all HTML tags
    clean = re.sub(r"<[^>]+>", " ", unescaped)
    # Collapse whitespace
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def _is_fresh(published: datetime, max_hours: int) -> bool:
    """Return True if the article was published within max_hours ago."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_hours)
    return published >= cutoff


# ---------------------------------------------------------------------------
# 1. RSS News Fetcher
# ---------------------------------------------------------------------------

class RSSFetcher:
    """
    Fetches articles from a list of standard RSS/Atom feeds using feedparser.

    Feeds configured: Yahoo Finance, CNBC Tech, MarketWatch, Reuters, Seeking Alpha.
    """

    def fetch(self) -> list[Article]:
        articles: list[Article] = []

        for label, url in Config.rss.FEEDS:
            logger.info("Fetching RSS feed: %s", label)
            try:
                feed = feedparser.parse(
                    url,
                    agent="StockBot/2.0",
                    request_headers={"Accept": "application/rss+xml, application/atom+xml, */*"},
                )

                if feed.bozo and not feed.entries:
                    # bozo=True means malformed XML, but entries may still exist
                    logger.warning(
                        "Feed '%s' returned malformed data: %s",
                        label, feed.bozo_exception
                    )

                for entry in feed.entries:
                    headline = (getattr(entry, "title", "") or "").strip()
                    summary = _clean_html(getattr(entry, "summary", "") or "")
                    url_link = getattr(entry, "link", "") or ""
                    published = _parse_datetime(getattr(entry, "published_parsed", None))

                    if not headline or not url_link:
                        continue
                    if not _is_fresh(published, Config.rss.MAX_AGE_HOURS):
                        continue

                    articles.append(Article(
                        id=_make_id(headline, label),
                        headline=headline,
                        summary=summary[:500],
                        source=label,
                        source_type="rss",
                        url=url_link,
                        published=published,
                    ))

            except Exception as exc:
                # Single feed failure must not stop other feeds
                logger.error("Failed to fetch RSS feed '%s': %s", label, exc)

        logger.info("RSSFetcher collected %d articles.", len(articles))
        return articles


# ---------------------------------------------------------------------------
# 2. Reddit RSS Fetcher
# ---------------------------------------------------------------------------

class RedditFetcher:
    """
    Fetches new posts from Reddit communities via their public .rss endpoints.

    Reddit does not require authentication for RSS. The description field
    contains raw HTML (Reddit markdown rendered to HTML), which is stripped
    to plain text before storage.

    Subreddits configured: r/stocks, r/investing, r/wallstreetbets
    """

    def fetch(self) -> list[Article]:
        articles: list[Article] = []

        for label, url in Config.reddit.FEEDS:
            logger.info("Fetching Reddit RSS: %s", label)
            try:
                feed = feedparser.parse(
                    url,
                    # Reddit RSS requires a descriptive User-Agent or returns 429
                    agent=Config.reddit.USER_AGENT,
                )

                if feed.get("status", 200) == 429:
                    logger.warning("Reddit rate-limited for %s. Skipping.", label)
                    continue

                for entry in feed.entries:
                    headline = (getattr(entry, "title", "") or "").strip()
                    # Reddit puts rendered HTML in the 'summary' or 'content' field
                    raw_summary = (
                        getattr(entry, "summary", "")
                        or (entry.get("content", [{}])[0].get("value", "") if hasattr(entry, "get") else "")
                        or ""
                    )
                    summary = _clean_html(raw_summary)
                    url_link = getattr(entry, "link", "") or ""
                    published = _parse_datetime(getattr(entry, "published_parsed", None))

                    if not headline or not url_link:
                        continue
                    if not _is_fresh(published, Config.reddit.MAX_AGE_HOURS):
                        continue

                    # Skip mod posts and stickied announcements
                    if headline.lower().startswith(("[mod]", "[weekly", "[daily", "[monthly")):
                        continue

                    articles.append(Article(
                        id=_make_id(headline, label),
                        headline=headline,
                        summary=summary[:500],
                        source=label,
                        source_type="reddit",
                        url=url_link,
                        published=published,
                    ))

            except Exception as exc:
                logger.error("Failed to fetch Reddit RSS '%s': %s", label, exc)

        logger.info("RedditFetcher collected %d posts.", len(articles))
        return articles


# ---------------------------------------------------------------------------
# 3. SEC EDGAR Fetcher
# ---------------------------------------------------------------------------

class SECFetcher:
    """
    Monitors SEC EDGAR for high-priority filings via its Atom RSS feed.

    Monitors:
      - Form 8-K  : Material events (earnings surprises, M&A, leadership changes)
      - Form 4    : Insider transactions (director/officer buys and sells)

    The EDGAR RSS endpoint returns the 20 most recent filings of each type.
    No API key is required; EDGAR only asks for a descriptive User-Agent.
    """

    def fetch(self) -> list[Article]:
        articles: list[Article] = []

        for form_type in Config.sec.FORM_TYPES:
            url = Config.sec.RECENT_FILINGS_URL.format(form_type=form_type)
            logger.info("Fetching SEC EDGAR filings: Form %s", form_type)

            try:
                feed = feedparser.parse(
                    url,
                    agent=Config.sec.USER_AGENT,
                )

                for entry in feed.entries:
                    headline = (getattr(entry, "title", "") or "").strip()
                    summary = _clean_html(getattr(entry, "summary", "") or "")
                    url_link = getattr(entry, "link", "") or ""
                    published = _parse_datetime(getattr(entry, "published_parsed", None))

                    if not headline or not url_link:
                        continue
                    if not _is_fresh(published, Config.sec.MAX_AGE_HOURS):
                        continue

                    # Prepend the filing type for clarity
                    if form_type not in headline:
                        headline = f"[{form_type}] {headline}"

                    articles.append(Article(
                        id=_make_id(headline, f"SEC-{form_type}"),
                        headline=headline,
                        summary=summary[:500],
                        source=f"SEC EDGAR ({form_type})",
                        source_type="sec",
                        url=url_link,
                        published=published,
                    ))

            except Exception as exc:
                logger.error("Failed to fetch SEC EDGAR form %s: %s", form_type, exc)

        logger.info("SECFetcher collected %d filings.", len(articles))
        return articles


# ---------------------------------------------------------------------------
# 4. Twitter / X Fetcher (tweety-ns, no API key required)
# ---------------------------------------------------------------------------

class TwitterFetcher:
    """
    Fetches recent tweets from high-signal financial accounts using tweety-ns.

    tweety-ns is an unofficial, reverse-engineered Twitter client that works
    without an official API key. It can operate in:
      - Guest mode  : no credentials, lower rate limits
      - Auth mode   : TWITTER_USERNAME + TWITTER_PASSWORD in .env (more reliable)

    Monitored accounts (configurable in config.py):
      @DeItaone       — breaking financial headlines aggregator
      @unusual_whales — unusual options/dark pool flow alerts
      @unusual_volume — unusual trading volume alerts
      @markets        — Bloomberg Markets
      @stockmktnews   — curated stock market news

    Graceful degradation
    --------------------
    If tweety-ns fails (import error, auth failure, rate limit), a warning is
    logged and an empty list is returned. The rest of the pipeline continues
    unaffected.
    """

    def __init__(self):
        self._client = None
        self._available = False
        self._init_client()

    def _init_client(self) -> None:
        """Attempt to initialise the tweety-ns client. Fail gracefully."""
        try:
            from tweety import Twitter

            client = Twitter("session")

            if Config.twitter.USERNAME and Config.twitter.PASSWORD:
                # Authenticated mode — more reliable, higher limits
                logger.info("Twitter: signing in as @%s …", Config.twitter.USERNAME)
                client.sign_in(Config.twitter.USERNAME, Config.twitter.PASSWORD)
                logger.info("Twitter: authenticated successfully.")
            else:
                # Guest / anonymous mode
                logger.info("Twitter: using guest mode (no credentials provided).")
                client.connect()

            self._client = client
            self._available = True

        except ImportError:
            logger.warning(
                "tweety-ns is not installed. Twitter source will be skipped.\n"
                "  → Install with: pip install tweety-ns"
            )
        except Exception as exc:
            logger.warning(
                "Twitter client initialisation failed: %s\n"
                "  → Twitter source will be skipped this session.",
                exc,
            )

    def fetch(self) -> list[Article]:
        if not self._available or self._client is None:
            return []

        articles: list[Article] = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=Config.rss.MAX_AGE_HOURS)

        for account in Config.twitter.ACCOUNTS:
            logger.info("Fetching tweets from @%s …", account)
            try:
                # tweety-ns returns Tweet objects with .text, .url, .date attrs
                tweets = self._client.get_tweets(
                    account,
                    pages=1,
                )

                # tweets may be a generator or list depending on version
                tweet_list = list(tweets)[:Config.twitter.TWEETS_PER_ACCOUNT]

                for tweet in tweet_list:
                    # Safely extract attributes (API shape varies by version)
                    text = str(getattr(tweet, "text", "") or "").strip()
                    tweet_url = str(getattr(tweet, "url", "") or getattr(tweet, "tweet_url", "") or "")
                    created = getattr(tweet, "date", None) or getattr(tweet, "created_on", None)

                    if not text:
                        continue

                    # Parse published timestamp
                    if isinstance(created, datetime):
                        published = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
                    elif isinstance(created, str):
                        published = _parse_datetime(created)
                    else:
                        published = datetime.now(timezone.utc)

                    if published < cutoff:
                        continue

                    articles.append(Article(
                        id=_make_id(text, f"twitter-{account}"),
                        headline=text[:280],   # Twitter char limit
                        summary="",
                        source=f"@{account}",
                        source_type="twitter",
                        url=tweet_url,
                        published=published,
                    ))

                # Be polite between account requests
                time.sleep(Config.twitter.REQUEST_DELAY)

            except Exception as exc:
                logger.warning(
                    "Failed to fetch tweets from @%s: %s. Skipping account.",
                    account, exc,
                )

        logger.info("TwitterFetcher collected %d tweets.", len(articles))
        return articles


# ---------------------------------------------------------------------------
# 5. Unified Scraper facade
# ---------------------------------------------------------------------------

class Scraper:
    """
    Orchestrates all four fetchers and returns a single deduplicated,
    time-sorted list of Articles.

    Each fetcher runs independently. Failures in one source do not prevent
    others from executing.
    """

    def __init__(self):
        self._rss = RSSFetcher()
        self._reddit = RedditFetcher()
        self._sec = SECFetcher()
        self._twitter = TwitterFetcher()

        logger.info("Scraper initialised with sources: RSS, Reddit, SEC EDGAR, Twitter/X")

    def fetch_all(self) -> list[Article]:
        """
        Fetch from all sources, deduplicate by Article.id, and return
        articles sorted newest-first.
        """
        seen: set[str] = set()
        combined: list[Article] = []

        sources = [
            ("RSS",     self._rss),
            ("Reddit",  self._reddit),
            ("SEC",     self._sec),
            ("Twitter", self._twitter),
        ]

        for name, fetcher in sources:
            try:
                items = fetcher.fetch()
                before = len(combined)
                for item in items:
                    if item.id not in seen:
                        seen.add(item.id)
                        combined.append(item)
                logger.info("%s: +%d unique articles.", name, len(combined) - before)
            except Exception as exc:
                logger.exception("Unexpected error in %s fetcher: %s", name, exc)

        combined.sort(key=lambda a: a.published, reverse=True)
        logger.info("Total unique articles fetched this cycle: %d", len(combined))
        return combined
