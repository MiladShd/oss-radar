"""Proof that the model actually consumes its training data every fit.

Same data + seed => reproducible model. Different data (a "new day" where the relationship
between features and outcome shifts) => a different model that has learned the new reality.
This guards against ever accidentally serving a frozen/stale model.
"""

import numpy as np
import pandas as pd

from oss_radar.features import DOWNLOAD_FEATURES
from oss_radar.models.growth import GrowthModel


def _dataset(seed: int, signal_on: str | None, n: int = 800) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({f: rng.normal(0, 1, n) for f in DOWNLOAD_FEATURES})
    df["feature_date"] = pd.date_range("2026-01-01", periods=n).date
    base = rng.normal(0, 0.1, n)
    # the outcome depends on a chosen feature ("today's reality"); None => pure noise
    df["growth_target_7d"] = (df[signal_on] * 1.2 + base) if signal_on else base
    return df


def test_same_data_same_model():
    df = _dataset(1, "mom_7v7")
    probe = _dataset(99, None)
    p1 = GrowthModel(seed=7)
    p1.fit(df)
    p2 = GrowthModel(seed=7)
    p2.fit(df)
    a = p1.predict(probe)
    b = p2.predict(probe)
    assert np.allclose(a, b), "same data + seed must reproduce the same model"


def test_new_data_changes_the_model():
    probe = _dataset(99, None)
    # "yesterday": momentum drives growth.  "today": volatility drives growth instead.
    m_yesterday = GrowthModel(seed=7)
    m_yesterday.fit(_dataset(1, "mom_7v7"))
    m_today = GrowthModel(seed=7)
    m_today.fit(_dataset(2, "volatility_28"))

    preds_changed = float(np.mean(np.abs(m_yesterday.predict(probe) - m_today.predict(probe))))
    assert preds_changed > 0.05, "retraining on different data must change predictions"

    # each model learned the feature that actually drove its own training data
    assert max(m_yesterday.importances, key=m_yesterday.importances.get) == "mom_7v7"
    assert max(m_today.importances, key=m_today.importances.get) == "volatility_28"


def test_metric_reflects_real_signal():
    # a model trained where a feature genuinely predicts the target scores far better
    # than one trained on pure noise — i.e. the metric tracks real signal, not luck.
    signal = GrowthModel(seed=7)
    m1 = signal.fit(_dataset(1, "mom_7v7"))
    noise = GrowthModel(seed=7)
    m2 = noise.fit(_dataset(1, None))
    assert m1["spearman"] > m2["spearman"] + 0.2
