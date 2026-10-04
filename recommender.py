"""Scores how promising a market-mover candidate looks, based on a
classifier trained from your accumulated 'Good pick' / 'False positive'
Telegram feedback (see market_alerts.py and poll_telegram_feedback.py).

Two features come from news_signals.py, kept separate rather than
folded into one flat "had news" flag:

- VALIDATION_SIGNAL_TYPES (price_movement, deck_tech_feature,
  combo_discovery) tend to appear alongside or after a move — they
  confirm something that's already happening.
- LEADING_SIGNAL_TYPES (spoiler_preview, banned_restricted,
  reprint_announcement) tend to precede a move and hint at a *future*
  direction instead.

Conflating them would risk teaching the model the wrong thing: a
reprint_announcement usually predicts a price *drop* (more supply into
circulation), not a rise, so it shouldn't share a coefficient with
price_movement just because both happen to correlate with "genuine
interest" in the abstract. news_signals.py itself sends nothing to
Telegram for either category on an already-detected mover — this is
the only place that coverage gets used, as inputs the model learns its
own weights for. (A *leading* signal on a card that ISN'T already a
mover is a different thing — see news_signals.py's early-warning check,
which does alert on those, since that's the one case where the "hint"
has nowhere else to surface.)

The window/effect size this relies on was validated by backtesting
real historical movers against a control group of non-movers before
building on it (70% of movers had verified prior coverage vs. 18% of
non-movers).

Deliberately does NOT filter which movers get alerted on — only a
watchlist you can react to produces feedback to learn from, so every
mover is still sent; this only adds a confidence annotation. The model
is retrained from scratch on every run rather than persisted between
runs — the feedback dataset is small enough (personal-scale) that this
is simpler than versioning a model file, and guarantees the score always
reflects every rating you've given so far.
"""

import logging
from datetime import datetime, timezone

import db

logger = logging.getLogger("tcg-price-checker")

MIN_LABELED_EXAMPLES = 10
NEWS_SIGNAL_WINDOW_DAYS = 60
VALIDATION_SIGNAL_TYPES = ("price_movement", "deck_tech_feature", "combo_discovery")
LEADING_SIGNAL_TYPES = ("spoiler_preview", "banned_restricted", "reprint_announcement")


def _feature_vector(price_before, pct_change, had_validation_signal, had_leading_signal):
    abs_change = price_before * pct_change / 100
    return [
        price_before,
        pct_change,
        abs_change,
        1.0 if had_validation_signal else 0.0,
        1.0 if had_leading_signal else 0.0,
    ]


def _news_features(card_name, before_timestamp):
    had_validation = db.had_recent_news_signal(card_name, before_timestamp, NEWS_SIGNAL_WINDOW_DAYS, VALIDATION_SIGNAL_TYPES)
    had_leading = db.had_recent_news_signal(card_name, before_timestamp, NEWS_SIGNAL_WINDOW_DAYS, LEADING_SIGNAL_TYPES)
    return had_validation, had_leading


def train():
    """Return a fitted classifier, or None if there isn't enough labeled
    feedback yet (or it's all one class) to train something meaningful."""
    rows = db.get_labeled_recommendations()
    if len(rows) < MIN_LABELED_EXAMPLES:
        logger.info(
            "Only %d labeled recommendation(s) so far (need %d) — sending alerts without a confidence score",
            len(rows),
            MIN_LABELED_EXAMPLES,
        )
        return None

    labels = {row["feedback"] for row in rows}
    if len(labels) < 2:
        logger.info("All feedback so far is '%s' — need both good and bad examples to train", labels)
        return None

    from sklearn.linear_model import LogisticRegression

    X = [
        _feature_vector(r["price_before"], r["pct_change"], *_news_features(r["card_name"], r["sent_at"]))
        for r in rows
    ]
    y = [1 if r["feedback"] == "good" else 0 for r in rows]

    model = LogisticRegression(max_iter=1000, class_weight="balanced")
    model.fit(X, y)
    logger.info("Trained recommender on %d labeled example(s)", len(rows))
    return model


def score(model, price_before, pct_change, card_name=None):
    """Predicted probability (0-100) that this candidate would be tagged
    a 'good pick', or None if no model is available yet. `card_name`
    looks up recent news coverage as a feature; omit it only if that
    lookup genuinely isn't possible for the caller."""
    if model is None:
        return None
    had_validation, had_leading = False, False
    if card_name:
        now = datetime.now(timezone.utc).isoformat()
        had_validation, had_leading = _news_features(card_name, now)
    proba = model.predict_proba([_feature_vector(price_before, pct_change, had_validation, had_leading)])[0][1]
    return round(proba * 100, 1)
