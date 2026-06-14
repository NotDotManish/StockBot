# 📈 StockBot v2 — Multi-Source Sentiment Alerting Bot

StockBot v2 aggregates financial intelligence from **five free, zero-subscription
sources**, runs every headline and post through **ProsusAI/FinBERT**, and fires
real-time alerts to **Discord** or **Telegram** when market-moving content is
detected.

**No API keys required** — all data sources are publicly accessible.

---

## ✨ What's New in v2

| Feature | v1 | v2 |
|---|---|---|
| News sources | Finnhub API / NewsAPI | RSS Feeds + Reddit + SEC + Twitter |
| API keys required | Yes (2 keys) | **None** |
| Reddit retail sentiment | ❌ | ✅ r/stocks, r/investing, r/wsb |
| SEC insider / 8-K filings | ❌ | ✅ 8-K + Form 4 live monitoring |
| Twitter/X signals | ❌ | ✅ @DeItaone, @unusual_whales + more |
| Sentiment threshold | 75% | **85%** (stricter, less spam) |
| High-impact keywords | 18 | **40+** (expanded) |
| Architecture | `modules/` sub-package | Flat: `scraper`, `sentiment`, `alerts` |

---

## 🗂 Project Structure

```
StockBot/
├── main.py         ← Entry point, APScheduler, pipeline orchestration
├── config.py       ← All configuration (reads from .env)
├── scraper.py      ← Data ingestion: RSS, Reddit, SEC EDGAR, Twitter/X
├── sentiment.py    ← ProsusAI/FinBERT analysis
├── alerts.py       ← AlertFilter + Telegram/Discord formatters
├── requirements.txt
├── .env.example
└── README.md
```

---

## 🚀 Quick Start

### 1. Set up the environment

```bash
cd StockBot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> **Note:** First run downloads FinBERT (~440 MB). Cached by HuggingFace after that.

### 2. Configure

```bash
cp .env.example .env
```

Open `.env`. The **only required settings** are your notification channel:

#### Option A — Discord (easiest)
1. Open your Discord server → **Settings → Integrations → Webhooks → New Webhook**
2. Copy the URL into `.env`:
```env
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
NOTIFIER=discord
```

#### Option B — Telegram
1. Message **[@BotFather](https://t.me/BotFather)** → `/newbot` → copy the token
2. Start a chat with your bot, then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat ID
```env
TELEGRAM_BOT_TOKEN=1234567890:ABCdef...
TELEGRAM_CHAT_ID=123456789
NOTIFIER=telegram
```

### 3. Run

```bash
python main.py
```

---

## 📡 Data Sources (All Free)

| Source | What it provides | Freshness |
|---|---|---|
| **Yahoo Finance RSS** | Market headlines | Live |
| **CNBC Tech RSS** | Tech sector news | Live |
| **MarketWatch RSS** | Top stories | Live |
| **Reuters Business RSS** | Global business news | Live |
| **Seeking Alpha RSS** | Stock analysis | Live |
| **r/stocks (RSS)** | Retail investor sentiment | ~Real-time |
| **r/investing (RSS)** | Retail investor discussions | ~Real-time |
| **r/wallstreetbets (RSS)** | High-volatility plays | ~Real-time |
| **SEC EDGAR — 8-K** | Material events (M&A, earnings surprises) | Live |
| **SEC EDGAR — Form 4** | Insider buy/sell transactions | Live |
| **@DeItaone (Twitter)** | Breaking market headlines | Live |
| **@unusual_whales (Twitter)** | Options flow & dark pool alerts | Live |

---

## 🔔 Alert Format

### Discord (Rich Embed)
```
🚨 SEC FILING  |  🔴 BEARISH (91.3%)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[8-K] MegaCorp Inc. files material event — CEO resignation

🔑 Matched Keywords
  `restructuring`  `layoffs`

📊 Sentiment Breakdown    ⚡ Triggered By
Positive: 2.1%            Strong sentiment +
Negative: 91.3%           keywords: "restructuring"
Neutral:  6.6%

SEC EDGAR (8-K)  ·  14:32 UTC  ·  StockBot v2
```

### Telegram (MarkdownV2)
```
🚨 SEC FILING · 🔴 BEARISH · strong (91.3%)

📋 [8-K] MegaCorp Inc. files material event — CEO resignation
🔑 Keywords: "restructuring", "layoffs"
🏢 SEC EDGAR (8-K) · 14:32 UTC
🔗 Read more
```

---

## ⚙️ Configuration Reference

All settings in `.env`:

```env
# Notifier channel
NOTIFIER=discord               # "telegram" | "discord" | "both"

# Sentiment threshold (0–1). 0.85 = only very strong signals
SENTIMENT_THRESHOLD=0.85

# How often the bot runs (minutes)
FETCH_INTERVAL_MINUTES=15

# Twitter/X credentials (optional — guest mode if blank)
TWITTER_USERNAME=
TWITTER_PASSWORD=

# Log verbosity
LOG_LEVEL=INFO
```

### Customising Keywords

Edit `FilterConfig.HIGH_IMPACT_KEYWORDS` in [`config.py`](config.py). Keywords
use whole-word regex matching (`"ipo"` won't match `"exposition"`).

### Customising Twitter Accounts

Edit `TwitterConfig.ACCOUNTS` in [`config.py`](config.py).

### Customising RSS Feeds

Edit `RSSConfig.FEEDS` in [`config.py`](config.py) — any standard RSS/Atom URL works.

---

## 🏗 Architecture

```
main.py  ── run_pipeline()
              │
              ├─ 1. Scraper.fetch_all()             scraper.py
              │       ├─ RSSFetcher         (Yahoo, CNBC, MarketWatch, Reuters, SA)
              │       ├─ RedditFetcher      (r/stocks, r/investing, r/wsb)
              │       ├─ SECFetcher         (8-K, Form 4 via EDGAR RSS)
              │       └─ TwitterFetcher     (@DeItaone, @unusual_whales …)
              │
              ├─ 2. SentimentAnalyzer.analyse()      sentiment.py
              │       └─ ProsusAI/FinBERT (batched)
              │
              ├─ 3. AlertFilter.evaluate_batch()     alerts.py
              │       ├─ Sentiment score >= 0.85
              │       └─ High-impact keyword match
              │
              └─ 4. Notifier.broadcast()             alerts.py
                      ├─ _TelegramChannel (MarkdownV2)
                      └─ _DiscordChannel  (Rich Embed)
```

---

## 🛡 Rate Limits

| Source | Limit | Bot usage |
|---|---|---|
| RSS feeds | Unlimited | ~5 requests / 15 min |
| Reddit RSS | ~600 req/10 min (IP-based) | 3 requests / 15 min |
| SEC EDGAR | 10 req/sec | 2 requests / 15 min |
| Twitter (guest) | ~100 req/15 min | ~5 requests / 15 min |
| Discord Webhook | 5 req / 2 sec | ≤ 8 messages / cycle |
| Telegram Bot API | 30 msg/sec | ≤ 8 messages / cycle |

---

## 🐞 Troubleshooting

| Symptom | Fix |
|---|---|
| `tweety-ns` auth error | Leave `TWITTER_USERNAME` blank to use guest mode |
| Reddit returns 429 | Reddit rate-limits by IP; increase `FETCH_INTERVAL_MINUTES` |
| FinBERT download fails | Check internet access; ensure `torch` is installed |
| No alerts after many cycles | Lower `SENTIMENT_THRESHOLD` to 0.70 in `.env` |
| Telegram parse error | Special characters in headline — check logs for the exact article |
| SEC feed returns empty | EDGAR occasionally has maintenance windows; bot retries next cycle |

---

## 📄 License

MIT — free to use, modify, and distribute.
