"""Scores how promising a market-mover candidate looks, based on a
classifier trained from your accumulated 'Good pick' / 'False positive'
Telegram feedback (see market_alerts.py and poll_telegram_feedback.py).

Deliberately does NOT filter which movers get alerted on — only a
watchlist you can react to produces feedback to learn from, so every
mover is still sent; this only adds a confidence annotation. The model
is retrained from scratch on every run rather than persisted between
runs — the feedback dataset is small enough (personal-scale) that this
is simpler than versioning a model file, and guarantees the score always
reflects every rating you've given so far.
"""

import logging

import db

logger = logging.getLogger("tcg-price-checker")

MIN_LABELED_EXAMPLES = 10


def _feature_vector(price_before, pct_change):
    abs_change = price_before * pct_change / 100
    return [price_before, pct_change, abs_change]


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

    X = [_feature_vector(r["price_before"], r["pct_change"]) for r in rows]
    y = [1 if r["feedback"] == "good" else 0 for r in rows]

    model = LogisticRegression(max_iter=1000, class_weight="balanced")
    model.fit(X, y)
    logger.info("Trained recommender on %d labeled example(s)", len(rows))
    return model


def score(model, price_before, pct_change):
    """Predicted probability (0-100) that this candidate would be tagged
    a 'good pick', or None if no model is available yet."""
    if model is None:
        return None
    proba = model.predict_proba([_feature_vector(price_before, pct_change)])[0][1]
    return round(proba * 100, 1)
