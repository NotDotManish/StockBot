"""
sentiment.py
============
Financial sentiment analysis using ahmedrachid/FinancialBERT-Sentiment-Analysis.

This is a fully modernized FinancialBERT model with a complete config.json
(including the required model_type field) and .safetensors weights. It outputs
three probability classes with all-lowercase labels:
    "positive"  → bullish signal
    "negative"  → bearish signal
    "neutral"   → no strong directional signal

This module:
  - Lazy-loads the HuggingFace pipeline on first use and caches it globally
  - Accepts a list of Article objects (from scraper.py)
  - Combines headline + summary snippet for richer context
  - Runs inference in configurable batches for efficiency
  - Returns (Article, SentimentResult) pairs
  - Falls back to individual inference if a batch fails

Error handling
--------------
A failed inference for one article assigns label="neutral", score=0.0 and
logs a warning. The rest of the batch continues unaffected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from config import Config

if TYPE_CHECKING:
    from scraper import Article

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class SentimentResult:
    """Sentiment output for a single Article."""

    label: str           # "positive" | "negative" | "neutral"  (lowercase from FinancialBERT)
    score: float         # Winning label's confidence in [0, 1]
    strength: str        # "strong" | "moderate" | "weak"
    raw_scores: dict[str, float] = field(default_factory=dict)

    # ---- Convenience properties -------------------------------------------

    @property
    def is_bullish(self) -> bool:
        return self.label == "positive"

    @property
    def is_bearish(self) -> bool:
        return self.label == "negative"

    @property
    def is_neutral(self) -> bool:
        return self.label == "neutral"

    @property
    def emoji(self) -> str:
        """Return a coloured circle that visually conveys direction."""
        if self.is_bullish:
            return "🟢"
        if self.is_bearish:
            return "🔴"
        return "⚪"

    @property
    def direction_label(self) -> str:
        """Upper-case human-readable direction."""
        return {
            # Lowercase keys match FinancialBERT-Sentiment-Analysis output
            "positive": "BULLISH",
            "negative": "BEARISH",
            "neutral":  "NEUTRAL",
        }.get(self.label, self.label.upper())

    def score_pct(self) -> str:
        """Formatted confidence percentage string, e.g. '91.3%'."""
        return f"{self.score * 100:.1f}%"


def _score_to_strength(score: float) -> str:
    if score >= 0.85:
        return "strong"
    if score >= 0.65:
        return "moderate"
    return "weak"


# ---------------------------------------------------------------------------
# Lazy model loader
# ---------------------------------------------------------------------------

_pipeline = None   # Module-level cache — survives across scheduler runs


def _load_model():
    """
    Load ProsusAI/finbert with an explicit two-step approach to ensure
    use_safetensors=False is honoured and the safetensors 404 crash is avoided.

    Why decoupled loading?
    ----------------------
    Passing model_kwargs={"use_safetensors": False} through pipeline() is
    unreliable — the flag can be swallowed depending on the transformers
    version. Loading the tokenizer and model explicitly with
    AutoModelForSequenceClassification.from_pretrained(..., use_safetensors=False)
    guarantees the flag reaches the right call site, skipping the .safetensors
    HEAD request that was triggering the huggingface_hub AttributeError.

    Label output: 'positive', 'negative', 'neutral'  (all lowercase).
    Normalised to lowercase in _parse_raw_output() for safety.
    """
    global _pipeline
    if _pipeline is not None:
        return _pipeline

    model_name = Config.sentiment.MODEL_NAME
    logger.info(
        "Loading '%s' — first run may take a moment …", model_name
    )

    try:
        from transformers import (
            AutoTokenizer,
            AutoModelForSequenceClassification,
            pipeline as hf_pipeline,
            logging as hf_logging,
        )

        # Suppress HuggingFace verbose download bars in production
        hf_logging.set_verbosity_error()

        # Step 1: load the tokenizer independently
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        logger.info("Tokenizer loaded.")

        # Step 2: load the model weights — use_safetensors=False tells the
        # loader to skip the .safetensors probe entirely and go straight to
        # the .bin checkpoint that this model ships with.
        model = AutoModelForSequenceClassification.from_pretrained(
            model_name,
            use_safetensors=False,
        )
        logger.info("Model weights loaded (PyTorch .bin).")

        # Step 3: build the pipeline from the pre-loaded objects, NOT the
        # model name string, so no further Hub requests are triggered.
        _pipeline = hf_pipeline(
            "text-classification",
            model=model,
            tokenizer=tokenizer,
            top_k=None,        # Return scores for ALL 3 labels
            max_length=Config.sentiment.MAX_LENGTH,
            truncation=True,
        )
        logger.info("ProsusAI/finbert pipeline ready.")

    except Exception as exc:
        raise RuntimeError(
            f"Could not load model '{model_name}': {exc}\n"
            "Ensure 'torch' and 'transformers' are installed and you have "
            "internet access for the first model download."
        ) from exc

    return _pipeline


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_raw_output(raw: list[dict]) -> SentimentResult:
    """
    Convert a FinBERT top_k=None output list into a SentimentResult.

    Labels are normalised to lowercase with .lower() before being stored.
    This makes every downstream comparison in SentimentResult and alerts.py
    model-agnostic — safe against any future casing change (e.g. 'Positive'
    vs 'positive') across model or library version upgrades.
    """
    # Normalise label casing to lowercase unconditionally
    scores: dict[str, float] = {
        item["label"].lower(): item["score"] for item in raw
    }
    best_label = max(scores, key=lambda k: scores[k])
    best_score = scores[best_label]
    return SentimentResult(
        label=best_label,
        score=round(best_score, 4),
        strength=_score_to_strength(best_score),
        raw_scores={k: round(v, 4) for k, v in scores.items()},
    )


def _fallback_result() -> SentimentResult:
    """Return a safe neutral result used when inference fails."""
    return SentimentResult(
        label="neutral",
        score=0.0,
        strength="weak",
        # Lowercase keys match FinancialBERT-Sentiment-Analysis label output
        raw_scores={"positive": 0.0, "negative": 0.0, "neutral": 1.0},
    )


# ---------------------------------------------------------------------------
# Analyser class
# ---------------------------------------------------------------------------

class SentimentAnalyzer:
    """
    Runs FinBERT inference on a list of Article objects.

    Usage:
        analyzer = SentimentAnalyzer()
        pairs = analyzer.analyse(articles)   # [(Article, SentimentResult), ...]
    """

    def __init__(self):
        # Load eagerly so model failures surface at startup, not mid-cycle
        self._pipeline = _load_model()

    def _build_input(self, article: "Article") -> str:
        """
        Combine headline + summary snippet.
        FinBERT performs best on concise, factual financial sentences.
        """
        if article.summary:
            return f"{article.headline}. {article.summary[:300]}"
        return article.headline

    def _single_inference(self, text: str) -> SentimentResult:
        """Run inference on a single text with full error handling."""
        try:
            raw: list[dict] = self._pipeline(text)[0]
            return _parse_raw_output(raw)
        except Exception as exc:
            logger.warning("Single inference failed for '%s…': %s", text[:60], exc)
            return _fallback_result()

    def analyse(
        self, articles: list["Article"]
    ) -> list[tuple["Article", SentimentResult]]:
        """
        Analyse a list of Articles in batches.

        Returns a list of (Article, SentimentResult) pairs in input order.
        """
        if not articles:
            return []

        results: list[tuple["Article", SentimentResult]] = []
        batch_size = Config.sentiment.BATCH_SIZE

        logger.info(
            "Analysing %d article(s) with FinBERT (batch_size=%d) …",
            len(articles), batch_size,
        )

        for batch_start in range(0, len(articles), batch_size):
            batch = articles[batch_start : batch_start + batch_size]
            texts = [self._build_input(a) for a in batch]

            raw_batch = None
            try:
                # The HuggingFace pipeline accepts a list for batch inference
                raw_batch = self._pipeline(texts)
            except Exception as exc:
                logger.warning(
                    "Batch inference failed (offset %d): %s — falling back to individual.",
                    batch_start, exc,
                )

            for i, article in enumerate(batch):
                if raw_batch is not None:
                    try:
                        result = _parse_raw_output(raw_batch[i])
                    except Exception:
                        result = self._single_inference(self._build_input(article))
                else:
                    result = self._single_inference(self._build_input(article))

                logger.debug(
                    "%s [%s|%.0f%%] %s",
                    result.emoji,
                    result.label,
                    result.score * 100,
                    article.headline[:70],
                )
                results.append((article, result))

        return results
