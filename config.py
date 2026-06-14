"""
config.py
=========
Centralised configuration for StockBot v2.

All tuneable values live here. Secrets (tokens, passwords) are read from
environment variables via python-dotenv. Everything else has a safe default.
"""

from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# ---------------------------------------------------------------------------
# RSS News Feeds
# ---------------------------------------------------------------------------

class RSSConfig:
    # Each entry is (human_label, feed_url)
    FEEDS: list[tuple[str, str]] = [
        ("Yahoo Finance",    "https://finance.yahoo.com/news/rssindex"),
        ("CNBC Tech",        "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
        ("MarketWatch",      "https://feeds.marketwatch.com/marketwatch/topstories/"),
        ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
        ("Seeking Alpha",    "https://seekingalpha.com/feed.xml"),
    ]

    # Discard articles older than this many hours
    MAX_AGE_HOURS: int = 6

    # feedparser request timeout in seconds
    TIMEOUT: int = 15


# ---------------------------------------------------------------------------
# Reddit (via RSS — no API key needed)
# ---------------------------------------------------------------------------

class RedditConfig:
    FEEDS: list[tuple[str, str]] = [
        ("r/stocks",    "https://www.reddit.com/r/stocks/new/.rss"),
        ("r/investing", "https://www.reddit.com/r/investing/new/.rss"),
        ("r/wallstreetbets", "https://www.reddit.com/r/wallstreetbets/new/.rss"),
    ]

    MAX_AGE_HOURS: int = 3   # Reddit moves fast — keep fresher window

    # feedparser User-Agent (Reddit requires a descriptive UA)
    USER_AGENT: str = "StockBot/2.0 (+https://github.com/stockbot)"


# ---------------------------------------------------------------------------
# SEC EDGAR Filings
# ---------------------------------------------------------------------------

class SECConfig:
    # EDGAR full-text search RSS — returns latest filings across all companies
    FEED_URL: str = "https://efts.sec.gov/LATEST/search-index?q=%22%22&dateRange=custom&startdt={date}&forms={forms}&_source=hits.hits._source.period_of_report,hits.hits._source.entity_name,hits.hits._source.file_date,hits.hits._source.form_type,hits.hits._source.biz_location,hits.hits._source.inc_states"

    # Simpler: EDGAR company search RSS (latest 8-K and Form 4 filings)
    RECENT_FILINGS_URL: str = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form_type}&dateb=&owner=include&count=20&search_text=&output=atom"

    # Filing types to monitor
    FORM_TYPES: list[str] = ["8-K", "4"]

    MAX_AGE_HOURS: int = 12  # SEC filings are less frequent

    # SEC EDGAR requires a descriptive User-Agent per their policy:
    # https://www.sec.gov/os/accessing-edgar-data
    USER_AGENT: str = "StockBot research@stockbot.local"


# ---------------------------------------------------------------------------
# Twitter / X
# ---------------------------------------------------------------------------

class TwitterConfig:
    # High-signal accounts to monitor (without @)
    ACCOUNTS: list[str] = [
        "DeItaone",        # Breaking market news aggregator
        "unusual_whales",  # Options flow & unusual activity
        "unusual_volume",  # Unusual trading volume alerts
        "markets",         # Bloomberg Markets
        "stockmktnews",    # Stock market news aggregator
    ]

    # How many recent tweets to fetch per account per cycle
    TWEETS_PER_ACCOUNT: int = 5

    # tweety-ns credentials (optional — guest mode used if blank)
    USERNAME: str = _optional("TWITTER_USERNAME")
    PASSWORD: str = _optional("TWITTER_PASSWORD")

    # Seconds to wait between account requests (be polite)
    REQUEST_DELAY: float = 2.0


# ---------------------------------------------------------------------------
# Sentiment Analysis
# ---------------------------------------------------------------------------

class SentimentConfig:
    # Local path to the manually downloaded FinBERT model directory.
    # AutoTokenizer and AutoModelForSequenceClassification accept a local
    # directory path exactly like a Hub model name — no network requests made.
    # Directory must contain: config.json, vocab.txt, pytorch_model.bin, etc.
    MODEL_NAME: str = "./finbert-model"
    MAX_LENGTH: int = 512
    BATCH_SIZE: int = 8

    # Only alert on non-neutral sentiment above this confidence
    THRESHOLD: float = float(_optional("SENTIMENT_THRESHOLD", "0.85"))


# ---------------------------------------------------------------------------
# Alert Filtering — High-Impact Keywords
# ---------------------------------------------------------------------------

class FilterConfig:
    HIGH_IMPACT_KEYWORDS: list[str] = [
        # M&A / Corporate
        "merger", "acquisition", "acquired", "takeover", "buyout",
        "tender offer", "spinoff", "spin-off", "divestiture",
        # Capital Events
        "ipo", "spac", "funding", "fundraising", "series a", "series b",
        "share buyback", "stock split", "dividend cut", "dividend increase",
        # Regulatory / Legal
        "fda approval", "fda approved", "fda rejection",
        "sec investigation", "sec charges", "antitrust",
        "class action", "lawsuit", "settlement",
        # Distress / Crisis
        "bankruptcy", "chapter 11", "chapter 7", "delisting",
        "default", "restructuring", "layoffs", "mass layoffs",
        # Insider / Unusual Activity
        "insider buying", "insider selling", "short squeeze",
        "short interest", "unusual options", "dark pool",
        # Tech / Innovation
        "quantum", "breakthrough", "patent", "recall",
        # Macro
        "fed rate", "interest rate", "inflation", "recession",
        "earnings beat", "earnings miss", "guidance raised", "guidance cut",
    ]


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

class NotifierConfig:
    CHANNEL: str = _optional("NOTIFIER", "discord")

    TELEGRAM_BOT_TOKEN: str = _optional("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID: str = _optional("TELEGRAM_CHAT_ID")
    DISCORD_WEBHOOK_URL: str = _optional("DISCORD_WEBHOOK_URL")

    # Hard cap on alerts per cycle to avoid flooding
    MAX_ALERTS_PER_CYCLE: int = 8


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class SchedulerConfig:
    FETCH_INTERVAL_MINUTES: int = int(_optional("FETCH_INTERVAL_MINUTES", "15"))
    JITTER_SECONDS: int = 45


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class LogConfig:
    LEVEL: str = _optional("LOG_LEVEL", "INFO").upper()
    LOG_FILE: str = "stockbot.log"


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

class Config:
    rss = RSSConfig()
    reddit = RedditConfig()
    sec = SECConfig()
    twitter = TwitterConfig()
    sentiment = SentimentConfig()
    filter = FilterConfig()
    notifier = NotifierConfig()
    scheduler = SchedulerConfig()
    log = LogConfig()
