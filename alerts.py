"""
alerts.py
=========
Filtering logic and notification dispatch for StockBot v2.

Two concerns are handled here:

  1. AlertFilter  — decides which (Article, SentimentResult) pairs deserve
                    an alert based on:
                      a) Sentiment confidence  >= SENTIMENT_THRESHOLD (0.85)
                      b) Presence of high-impact keywords in headline/summary

  2. Notifier     — formats and dispatches alert messages to Discord and/or
                    Telegram.  Source type drives the emoji and colour:

        Source      Emoji    Discord colour
        ─────────────────────────────────────
        rss         📰       #3498db (blue)
        reddit      🔴       #FF4500 (Reddit orange)
        sec         🚨       #E67E22 (amber)
        twitter     🐦       #1DA1F2 (Twitter blue)

Message anatomy (Telegram plain text):
    ─────────────────────────────────
    🚨 SEC FILING  ·  🔴 BEARISH · strong (91.3%)
    ─────────────────────────────────
    📋 [8-K] Apple Inc. files material event — CFO resignation
    🔑 Keywords: "bankruptcy", "restructuring"
    🏢 SEC EDGAR (8-K)  ·  14:32 UTC
    🔗 https://...
    ─────────────────────────────────

Error handling
--------------
Individual send failures are caught and logged. A broken notifier never
crashes the pipeline — the scheduler simply continues to the next cycle.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timezone
from typing import TYPE_CHECKING

import requests

from config import Config

if TYPE_CHECKING:
    from scraper import Article
    from sentiment import SentimentResult

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 10   # seconds for webhook POST calls


# ---------------------------------------------------------------------------
# Source metadata
# ---------------------------------------------------------------------------

SOURCE_EMOJI: dict[str, str] = {
    "rss":     "📰",
    "reddit":  "🔴",
    "sec":     "🚨",
    "twitter": "🐦",
}

SOURCE_LABEL: dict[str, str] = {
    "rss":     "NEWS",
    "reddit":  "REDDIT",
    "sec":     "SEC FILING",
    "twitter": "X / TWITTER",
}

# Discord embed sidebar colours (hex int)
DISCORD_SOURCE_COLOUR: dict[str, int] = {
    "rss":     0x3498DB,   # blue
    "reddit":  0xFF4500,   # Reddit orange
    "sec":     0xE67E22,   # amber
    "twitter": 0x1DA1F2,   # Twitter blue
}

# Override colour with sentiment if sentiment is non-neutral.
# Keys are all-lowercase to match ahmedrachid/FinancialBERT-Sentiment-Analysis label output.
DISCORD_SENTIMENT_COLOUR: dict[str, int] = {
    "positive": 0x2ECC71,  # green (bullish)
    "negative": 0xE74C3C,  # red (bearish)
    "neutral":  0x95A5A6,  # grey
}


# ---------------------------------------------------------------------------
# 1. Alert Filter
# ---------------------------------------------------------------------------

@dataclass
class FilterResult:
    """Records why (or why not) an article triggered an alert."""

    should_alert: bool
    matched_keywords: list[str] = field(default_factory=list)
    triggered_by_sentiment: bool = False
    triggered_by_keyword: bool = False

    @property
    def trigger_summary(self) -> str:
        parts = []
        if self.triggered_by_sentiment:
            parts.append("strong sentiment")
        if self.triggered_by_keyword:
            kws = ", ".join(f'"{k}"' for k in self.matched_keywords)
            parts.append(f"keywords: {kws}")
        return " + ".join(parts) if parts else "no trigger"


def _compile_keyword_patterns(keywords: list[str]) -> list[tuple[str, re.Pattern]]:
    """Pre-compile whole-word, case-insensitive patterns for each keyword."""
    return [
        (kw, re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE))
        for kw in keywords
    ]


# Compile once at import time
_KW_PATTERNS: list[tuple[str, re.Pattern]] = _compile_keyword_patterns(
    Config.filter.HIGH_IMPACT_KEYWORDS
)


def _find_keywords(text: str) -> list[str]:
    """Return all configured keywords found in *text*."""
    return [kw for kw, pat in _KW_PATTERNS if pat.search(text)]


class AlertFilter:
    """
    Evaluates (Article, SentimentResult) pairs and returns only those
    that meet at least one alert trigger condition.
    """

    def __init__(self):
        self._threshold = Config.sentiment.THRESHOLD
        logger.info(
            "AlertFilter ready — threshold=%.0f%%, keywords=%d",
            self._threshold * 100,
            len(Config.filter.HIGH_IMPACT_KEYWORDS),
        )

    def evaluate(
        self,
        article: "Article",
        sentiment: "SentimentResult",
    ) -> FilterResult:
        text = f"{article.headline} {article.summary}"
        matched = _find_keywords(text)

        by_sentiment = (
            not sentiment.is_neutral
            and sentiment.score >= self._threshold
        )
        by_keyword = bool(matched)

        result = FilterResult(
            should_alert=by_sentiment or by_keyword,
            matched_keywords=matched,
            triggered_by_sentiment=by_sentiment,
            triggered_by_keyword=by_keyword,
        )

        level = logging.DEBUG
        icon = "⏭ "
        if result.should_alert:
            level = logging.INFO
            icon = "✅"

        logger.log(
            level,
            "%s [%s] %s (%.0f%%) | kw=%s",
            icon,
            article.source_type.upper(),
            article.headline[:65],
            sentiment.score * 100,
            matched or "—",
        )
        return result

    def evaluate_batch(
        self,
        pairs: list[tuple["Article", "SentimentResult"]],
    ) -> list[tuple["Article", "SentimentResult", FilterResult]]:
        """
        Evaluate all pairs and return only those that should trigger an alert.
        Logs a summary line at the end.
        """
        all_triples = [
            (art, sent, self.evaluate(art, sent))
            for art, sent in pairs
        ]
        alerts = [(a, s, f) for a, s, f in all_triples if f.should_alert]

        logger.info(
            "Filter: %d/%d articles will generate alerts.",
            len(alerts), len(pairs),
        )
        return alerts


# ---------------------------------------------------------------------------
# 2. Formatters
# ---------------------------------------------------------------------------

def _format_time(article: "Article") -> str:
    pub = article.published
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    return pub.strftime("%H:%M UTC")


# ---- Telegram MarkdownV2 --------------------------------------------------

_MD2_SPECIAL = r"\_*[]()~`>#+-=|{}.!"


def _esc(text: str) -> str:
    """Escape a string for Telegram MarkdownV2."""
    for ch in _MD2_SPECIAL:
        text = text.replace(ch, f"\\{ch}")
    return text


def build_telegram_message(
    article: "Article",
    sentiment: "SentimentResult",
    filter_result: "FilterResult",
) -> str:
    src_emoji = SOURCE_EMOJI.get(article.source_type, "📄")
    src_label = SOURCE_LABEL.get(article.source_type, article.source_type.upper())

    header = _esc(
        f"{src_emoji} {src_label}  ·  "
        f"{sentiment.emoji} {sentiment.direction_label} · "
        f"{sentiment.strength} ({sentiment.score_pct()})"
    )
    headline = _esc(article.headline)
    source_line = _esc(f"{article.source}  ·  {_format_time(article)}")

    lines = [
        f"*{header}*",
        "",
        f"📋 {headline}",
    ]

    if filter_result.matched_keywords:
        kws = _esc(", ".join(f'"{k}"' for k in filter_result.matched_keywords))
        lines.append(f"🔑 Keywords: {kws}")

    lines.append(f"🏢 {source_line}")

    if article.url:
        lines.append(f"[🔗 Read more]({article.url})")

    return "\n".join(lines)


# ---- Discord Embed --------------------------------------------------------

def build_discord_embed(
    article: "Article",
    sentiment: "SentimentResult",
    filter_result: "FilterResult",
) -> dict:
    src_emoji = SOURCE_EMOJI.get(article.source_type, "📄")
    src_label = SOURCE_LABEL.get(article.source_type, article.source_type.upper())

    # Use sentiment colour for strong signals, source colour otherwise.
    # .lower() guards against any model version returning mixed-case labels.
    if sentiment.score >= Config.sentiment.THRESHOLD:
        colour = DISCORD_SENTIMENT_COLOUR.get(sentiment.label.lower(), 0x95A5A6)
    else:
        colour = DISCORD_SOURCE_COLOUR.get(article.source_type, 0x95A5A6)

    title = f"{src_emoji} {src_label}  |  {sentiment.emoji} {sentiment.direction_label} ({sentiment.score_pct()})"

    fields = []

    if filter_result.matched_keywords:
        fields.append({
            "name": "🔑 Matched Keywords",
            "value": "  ".join(f"`{k}`" for k in filter_result.matched_keywords),
            "inline": False,
        })

    if sentiment.raw_scores:
        score_lines = "\n".join(
            f"**{k.capitalize()}**: {v*100:.1f}%"
            for k, v in sentiment.raw_scores.items()
        )
        fields.append({
            "name": "📊 Sentiment Breakdown",
            "value": score_lines,
            "inline": True,
        })

    fields.append({
        "name": "⚡ Triggered By",
        "value": filter_result.trigger_summary.capitalize(),
        "inline": True,
    })

    return {
        "title": title[:256],
        "description": (article.headline if len(article.headline) <= 256 else article.headline[:253] + "…"),
        "url": article.url or None,
        "color": colour,
        "fields": fields,
        "footer": {
            "text": f"{article.source}  ·  {_format_time(article)}  ·  StockBot v2"
        },
    }


# ---------------------------------------------------------------------------
# 3. Notifier
# ---------------------------------------------------------------------------

class _TelegramChannel:
    """Dispatches MarkdownV2 messages via the Telegram Bot API."""

    def __init__(self, token: str, chat_id: str):
        if not token or not chat_id:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env "
                "to use the Telegram notifier."
            )
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id

    def send(
        self,
        article: "Article",
        sentiment: "SentimentResult",
        filter_result: "FilterResult",
    ) -> bool:
        message = build_telegram_message(article, sentiment, filter_result)
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": False,
        }

        for attempt in range(1, 3):
            try:
                resp = requests.post(self._url, json=payload, timeout=_HTTP_TIMEOUT)

                if resp.status_code == 200:
                    logger.info("Telegram ✅ '%s'", article.headline[:50])
                    return True

                if resp.status_code == 429:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 30)
                    logger.warning("Telegram rate-limited. Waiting %ds …", retry_after)
                    time.sleep(retry_after)
                    continue

                logger.error("Telegram HTTP %d: %s", resp.status_code, resp.text[:200])
                return False

            except requests.RequestException as exc:
                logger.warning("Telegram request failed (attempt %d/2): %s", attempt, exc)
                if attempt < 2:
                    time.sleep(5)

        return False


class _DiscordChannel:
    """Dispatches rich embed messages via a Discord Webhook."""

    def __init__(self, webhook_url: str):
        if not webhook_url or "discord.com" not in webhook_url:
            raise ValueError(
                "DISCORD_WEBHOOK_URL is not set or invalid. "
                "Create a webhook in Discord Server Settings → Integrations → Webhooks."
            )
        self._url = webhook_url

    def send(
        self,
        article: "Article",
        sentiment: "SentimentResult",
        filter_result: "FilterResult",
    ) -> bool:
        embed = build_discord_embed(article, sentiment, filter_result)
        payload = {"embeds": [embed]}

        for attempt in range(1, 3):
            try:
                resp = requests.post(self._url, json=payload, timeout=_HTTP_TIMEOUT)

                if resp.status_code in (200, 204):
                    logger.info("Discord ✅ '%s'", article.headline[:50])
                    return True

                if resp.status_code == 429:
                    retry_after = resp.json().get("retry_after", 5.0)
                    logger.warning("Discord rate-limited. Waiting %.1fs …", retry_after)
                    time.sleep(float(retry_after))
                    continue

                logger.error("Discord HTTP %d: %s", resp.status_code, resp.text[:200])
                return False

            except requests.RequestException as exc:
                logger.warning("Discord request failed (attempt %d/2): %s", attempt, exc)
                if attempt < 2:
                    time.sleep(5)

        return False


class Notifier:
    """
    Unified notifier that dispatches to one or both configured channels.

    Channel selection is driven by NOTIFIER env var: "telegram" | "discord" | "both".
    """

    def __init__(self):
        channel = Config.notifier.CHANNEL.lower()
        self._channels: list[_TelegramChannel | _DiscordChannel] = []

        if channel in ("telegram", "both"):
            self._channels.append(
                _TelegramChannel(
                    Config.notifier.TELEGRAM_BOT_TOKEN,
                    Config.notifier.TELEGRAM_CHAT_ID,
                )
            )

        if channel in ("discord", "both"):
            self._channels.append(
                _DiscordChannel(Config.notifier.DISCORD_WEBHOOK_URL)
            )

        if not self._channels:
            raise ValueError(
                f"NOTIFIER='{channel}' is invalid. Use 'telegram', 'discord', or 'both'."
            )

        logger.info(
            "Notifier ready: %s",
            [type(c).__name__ for c in self._channels],
        )

    def send(
        self,
        article: "Article",
        sentiment: "SentimentResult",
        filter_result: "FilterResult",
    ) -> None:
        """Send an alert to all configured channels. Never raises."""
        for channel in self._channels:
            try:
                channel.send(article, sentiment, filter_result)
            except Exception as exc:
                logger.exception(
                    "Unexpected error in %s.send(): %s", type(channel).__name__, exc
                )

    def broadcast(
        self,
        alerts: list[tuple["Article", "SentimentResult", "FilterResult"]],
    ) -> None:
        """
        Send alerts for a list of triples. Caps at MAX_ALERTS_PER_CYCLE
        to prevent channel flooding.
        """
        cap = Config.notifier.MAX_ALERTS_PER_CYCLE
        if len(alerts) > cap:
            logger.warning(
                "%d alerts in queue — capping at %d to avoid flooding.",
                len(alerts), cap,
            )
            alerts = alerts[:cap]

        for article, sentiment, filter_result in alerts:
            self.send(article, sentiment, filter_result)
