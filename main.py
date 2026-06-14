"""
main.py
=======
StockBot v2 — Entry Point & Scheduler
======================================

Wires together all four modules and runs the pipeline on a schedule:

    scraper.py   →  sentiment.py  →  alerts.py (filter)  →  alerts.py (notify)

Pipeline (one cycle):
    1. Scraper.fetch_all()              — pull from RSS, Reddit, SEC, Twitter
    2. SentimentAnalyzer.analyse()      — run FinBERT on every new article
    3. AlertFilter.evaluate_batch()     — apply threshold + keyword rules
    4. Notifier.broadcast()             — push formatted alerts to Discord/Telegram

Deduplication:
    An in-memory set (_seen_ids) tracks article IDs that have already been
    processed during the current process session. This prevents the same
    article from generating a duplicate alert across consecutive cycles.

Run:
    python main.py

Stop:
    Press Ctrl-C — the scheduler shuts down cleanly.
"""

from __future__ import annotations

import logging
import signal
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# ---------------------------------------------------------------------------
# Logging must be configured before any project module is imported
# ---------------------------------------------------------------------------

from config import Config


def _configure_logging() -> None:
    """Set up coloured console logging + rotating file logging."""
    level = getattr(logging, Config.log.LEVEL, logging.INFO)

    fmt = "%(asctime)s  %(levelname)-8s  %(name)-28s  %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"

    # Console handler — use colorlog if available, plain otherwise
    try:
        import colorlog
        console_hdlr = colorlog.StreamHandler()
        console_hdlr.setFormatter(
            colorlog.ColoredFormatter(
                "%(log_color)s" + fmt,
                datefmt=date_fmt,
                log_colors={
                    "DEBUG":    "cyan",
                    "INFO":     "green",
                    "WARNING":  "yellow",
                    "ERROR":    "red",
                    "CRITICAL": "bold_red",
                },
            )
        )
    except ImportError:
        console_hdlr = logging.StreamHandler()
        console_hdlr.setFormatter(logging.Formatter(fmt, datefmt=date_fmt))

    # Rotating file handler (5 MB × 3 backups)
    file_hdlr = RotatingFileHandler(
        Config.log.LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_hdlr.setFormatter(logging.Formatter(fmt, datefmt=date_fmt))

    logging.basicConfig(level=level, handlers=[console_hdlr, file_hdlr])

    # Silence noisy third-party loggers
    for lib in ("urllib3", "httpx", "asyncio", "apscheduler", "tweety"):
        logging.getLogger(lib).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Import project modules (after logging is configured)
# ---------------------------------------------------------------------------

from scraper import Scraper
from sentiment import SentimentAnalyzer
from alerts import AlertFilter, Notifier

logger = logging.getLogger("stockbot.main")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

# Tracks article IDs already alerted on during this process session.
# Prevents duplicate alerts across consecutive 15-minute cycles.
_seen_ids: set[str] = set()

# Module singletons — initialised in main(), reused across scheduler runs
_scraper: Scraper | None = None
_analyzer: SentimentAnalyzer | None = None
_filter: AlertFilter | None = None
_notifier: Notifier | None = None
_scheduler: BlockingScheduler | None = None


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def run_pipeline() -> None:
    """
    Execute one full scrape → analyse → filter → notify cycle.

    All top-level exceptions are caught so the scheduler keeps running
    even if an individual cycle encounters an unexpected error.
    """
    cycle_start = datetime.now(timezone.utc)
    logger.info(
        "━" * 62 + "\n  ⏱  Cycle started  %s\n" + "━" * 62,
        cycle_start.strftime("%Y-%m-%d %H:%M:%S UTC"),
    )

    try:
        # ── Step 1: Scrape ──────────────────────────────────────────────── #
        logger.info("Step 1/4  Scraping all sources …")
        all_articles = _scraper.fetch_all()

        # Filter to only articles we haven't alerted on yet
        new_articles = [a for a in all_articles if a.id not in _seen_ids]
        logger.info(
            "  → %d total  /  %d new (unseen this session)",
            len(all_articles), len(new_articles),
        )

        if not new_articles:
            logger.info("Nothing new to process — cycle complete.")
            return

        # ── Step 2: Sentiment ───────────────────────────────────────────── #
        logger.info("Step 2/4  Running FinBERT sentiment analysis …")
        scored_pairs = _analyzer.analyse(new_articles)

        # ── Step 3: Filter ──────────────────────────────────────────────── #
        logger.info("Step 3/4  Applying alert filter …")
        alert_triples = _filter.evaluate_batch(scored_pairs)

        # ── Step 4: Notify ──────────────────────────────────────────────── #
        if alert_triples:
            logger.info("Step 4/4  Broadcasting %d alert(s) …", len(alert_triples))
            _notifier.broadcast(alert_triples)
        else:
            logger.info("Step 4/4  No alerts to send this cycle.")

        # Mark all newly-seen articles to prevent repeat alerts
        for article in new_articles:
            _seen_ids.add(article.id)

        # Keep the seen-IDs set from growing unboundedly (cap at 20k)
        if len(_seen_ids) > 20_000:
            overflow = list(_seen_ids)[:10_000]
            for id_ in overflow:
                _seen_ids.discard(id_)
            logger.debug("Pruned seen-IDs set → %d entries remaining.", len(_seen_ids))

    except Exception as exc:
        logger.exception("Unhandled exception in pipeline cycle: %s", exc)

    finally:
        elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        logger.info("Cycle finished in %.1f seconds.\n", elapsed)


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------

def _print_banner() -> None:
    console = Console()
    t = Text()
    t.append("StockBot v2\n", style="bold cyan")
    t.append("Multi-Source Sentiment Alerting Bot\n\n", style="dim")
    t.append(f"  Sources    : RSS Feeds, Reddit, SEC EDGAR, Twitter/X\n", style="white")
    t.append(f"  Notifier   : {Config.notifier.CHANNEL.upper()}\n", style="white")
    t.append(f"  Threshold  : {Config.sentiment.THRESHOLD * 100:.0f}% confidence\n", style="white")
    t.append(f"  Interval   : every {Config.scheduler.FETCH_INTERVAL_MINUTES} min\n", style="white")
    t.append(f"  Keywords   : {len(Config.filter.HIGH_IMPACT_KEYWORDS)} configured\n", style="white")
    t.append(f"  Log file   : {Config.log.LOG_FILE}\n", style="white")
    console.print(Panel(t, border_style="cyan", padding=(1, 4)))


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

def _handle_signal(signum: int, _frame) -> None:
    """Handle Ctrl-C / SIGTERM cleanly."""
    logger.info("Signal %d received — shutting down …", signum)
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global _scraper, _analyzer, _filter, _notifier, _scheduler

    _configure_logging()
    _print_banner()

    # ── Initialise modules ──────────────────────────────────────────────── #
    logger.info("Initialising modules …")

    # Scraper (all sources — Twitter failure is non-fatal)
    try:
        _scraper = Scraper()
        logger.info("✅ Scraper ready.")
    except Exception as exc:
        logger.critical("Scraper failed to initialise: %s", exc)
        sys.exit(1)

    # FinBERT (must download model on first run)
    try:
        _analyzer = SentimentAnalyzer()
        logger.info("✅ SentimentAnalyzer ready.")
    except RuntimeError as exc:
        logger.critical("FinBERT failed to load: %s", exc)
        sys.exit(1)

    # Filter (no I/O — always succeeds)
    _filter = AlertFilter()
    logger.info("✅ AlertFilter ready.")

    # Notifier (validates that at least one channel is configured)
    try:
        _notifier = Notifier()
        logger.info("✅ Notifier ready.")
    except ValueError as exc:
        logger.critical("Notifier failed to initialise: %s", exc)
        sys.exit(1)

    # ── Run immediately, then schedule ──────────────────────────────────── #
    logger.info("Running first pipeline cycle immediately …")
    run_pipeline()

    # ── APScheduler setup ───────────────────────────────────────────────── #
    _scheduler = BlockingScheduler(timezone="UTC")
    _scheduler.add_job(
        func=run_pipeline,
        trigger=IntervalTrigger(
            minutes=Config.scheduler.FETCH_INTERVAL_MINUTES,
            jitter=Config.scheduler.JITTER_SECONDS,
        ),
        id="stockbot_pipeline",
        name="StockBot v2 Pipeline",
        replace_existing=True,
        max_instances=1,   # Never run two cycles simultaneously
        coalesce=True,     # If a run was missed, execute it once (not multiple times)
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info(
        "Scheduler running. Next cycle in ~%d min. Press Ctrl-C to stop.",
        Config.scheduler.FETCH_INTERVAL_MINUTES,
    )

    try:
        _scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("StockBot stopped cleanly.")


if __name__ == "__main__":
    main()
