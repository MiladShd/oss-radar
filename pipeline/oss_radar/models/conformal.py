"""Drift-aware conformal prediction intervals.

Split conformal prediction wraps any point forecast ŷ(x) in an interval ŷ ± q̂ whose half-width q̂ is a
quantile of held-out absolute residuals. Its coverage guarantee needs calibration and future errors to be
*exchangeable*. Package download growth is not: the model's error rises steadily across forecast origins
(docs/CONFORMAL.md), so a plain calibration under-covers badly (59% realised vs 80% nominal on the
production history). Two standard remedies are combined here:

1. **Recency weighting** (Barber, Candès, Ramdas & Tibshirani 2023, "Conformal prediction beyond
   exchangeability"): residuals from date ``d`` get weight ``2 ** (-age / half_life)`` where ``age`` counts
   forecast-origin dates back from the newest, so the quantile tracks the current error regime.
2. **Adaptive Conformal Inference** (Gibbs & Candès 2021): the working miscoverage level α_t is updated from
   observed misses, ``α_{t+1} = α_t + γ (α − err_t)``. Missing more than intended lowers α_t (wider
   intervals); missing less raises it. Here ``err_t`` is the *fraction* of a date's rows that missed
   (a batch version of ACI, because 91 packages resolve per origin date), and α_t is clipped to
   [0.01, 0.5]. **This is a heuristic adaptation, not the published algorithm**: clipping and batching break
   the recursion the long-run coverage theorem relies on, and under unbounded drift the intervals can still
   miss almost everything. Treat measured forward-chained coverage as the only evidence.

Pure NumPy so the lean dashboard image can import it without LightGBM.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

_ALPHA_MIN, _ALPHA_MAX = 0.01, 0.5


def weighted_conformal_quantile(
    scores: np.ndarray, weights: np.ndarray, level: float
) -> float:
    """Finite-sample weighted quantile of ``scores`` at ``level`` (0 < level < 1).

    The future point carries an unknown score, so it is treated as a point mass at +∞ with weight
    ``max(weights)``: the quantile of ``Σ wᵢ δ_{sᵢ} + w_max δ_{+∞}`` normalised to one. Returns ``inf``
    when the calibration set is too small to support the level.
    """
    scores = np.asarray(scores, dtype=float)
    weights = np.asarray(weights, dtype=float)
    if scores.size == 0 or not 0 < level < 1:
        return math.inf
    order = np.argsort(scores)
    s, w = scores[order], weights[order]
    cum = np.cumsum(w) / (w.sum() + w.max())
    idx = int(np.searchsorted(cum, level, side="left"))
    return float(s[idx]) if idx < s.size else math.inf


def recency_weights(date_index: np.ndarray, newest: int, half_life: float) -> np.ndarray:
    """``2 ** (-age / half_life)`` with age measured in forecast-origin dates (0 = newest)."""
    age = newest - np.asarray(date_index, dtype=float)
    return np.power(0.5, age / half_life)


@dataclass
class ForwardChainResult:
    """Honest coverage estimate: each date is predicted using only strictly earlier dates."""

    coverage: float
    mean_width: float
    n_scored: int
    n_dates_scored: int
    final_alpha: float
    per_date: list[dict] = field(default_factory=list)


def forward_chain(
    abs_residuals: np.ndarray,
    date_index: np.ndarray,
    *,
    alpha: float,
    half_life: float = 2.0,
    gamma: float = 0.1,
    warmup_dates: int = 3,
) -> ForwardChainResult:
    """Run recency-weighted conformal + batch ACI forward through time.

    ``date_index`` holds consecutive integers (0 = oldest origin date). The first ``warmup_dates``
    dates only seed the calibration set. The evaluation is *lag-free*: it assumes the previous date's
    outcomes are known when predicting the next. Production scoring has a ~70-day outcome lag, so the
    realised live coverage can be lower — see docs/CONFORMAL.md.
    """
    r = np.asarray(abs_residuals, dtype=float)
    d = np.asarray(date_index, dtype=int)
    dates = np.unique(d)
    alpha_t = alpha
    hits_total = scored = 0
    widths: list[float] = []
    per_date: list[dict] = []
    for k in dates[warmup_dates:]:
        cal = d < k
        weights = recency_weights(d[cal], k - 1, half_life)
        q = weighted_conformal_quantile(r[cal], weights, 1.0 - alpha_t)
        target = d == k
        if not math.isfinite(q):
            # The calibration set cannot support this level yet: no interval is available, so the date is
            # not scored rather than being silently covered by an arbitrary width.
            per_date.append({"date_index": int(k), "alpha_t": round(alpha_t, 4),
                             "half_width": None, "coverage": None})
            continue
        hit = r[target] <= q
        hits_total += int(hit.sum())
        scored += int(target.sum())
        widths.append(2 * q)
        miss_rate = 1.0 - float(hit.mean())
        per_date.append({
            "date_index": int(k), "alpha_t": round(alpha_t, 4),
            "half_width": round(q, 4), "coverage": round(1 - miss_rate, 4),
        })
        alpha_t = float(np.clip(alpha_t + gamma * (alpha - miss_rate), _ALPHA_MIN, _ALPHA_MAX))
    return ForwardChainResult(
        coverage=hits_total / scored if scored else float("nan"),
        mean_width=float(np.mean(widths)) if widths else float("nan"),
        n_scored=scored,
        n_dates_scored=len(widths),
        final_alpha=alpha_t,
        per_date=per_date,
    )


@dataclass
class DriftAwareConformal:
    """Deployable calibrator: holds the residual pool and the ACI state for one nominal level."""

    alpha: float = 0.2
    half_life: float = 2.0
    gamma: float = 0.1
    alpha_t: float = 0.2
    residuals: list[float] = field(default_factory=list)
    date_index: list[int] = field(default_factory=list)

    @classmethod
    def calibrate(
        cls,
        abs_residuals: np.ndarray,
        date_index: np.ndarray,
        *,
        alpha: float = 0.2,
        half_life: float = 2.0,
        gamma: float = 0.1,
        warmup_dates: int = 3,
    ) -> tuple[DriftAwareConformal, ForwardChainResult]:
        """Fit on out-of-sample residuals; returns the calibrator and its forward-chain evidence."""
        d = np.asarray(date_index, dtype=int)
        d = d - d.min()
        evidence = forward_chain(
            abs_residuals, d, alpha=alpha, half_life=half_life, gamma=gamma, warmup_dates=warmup_dates
        )
        cal = cls(
            alpha=alpha, half_life=half_life, gamma=gamma, alpha_t=evidence.final_alpha,
            residuals=[float(x) for x in np.asarray(abs_residuals, dtype=float)],
            date_index=[int(x) for x in d],
        )
        return cal, evidence

    def half_width(self) -> float:
        """Interval half-width q̂ for the *next* (not-yet-seen) forecast origin; ``inf`` if unsupported."""
        if not self.residuals:
            return math.inf
        r = np.asarray(self.residuals)
        d = np.asarray(self.date_index)
        weights = recency_weights(d, int(d.max()), self.half_life)
        return weighted_conformal_quantile(r, weights, 1.0 - self.alpha_t)  # inf => unsupported level

    def interval(self, point: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        q = self.half_width()
        p = np.asarray(point, dtype=float)
        return p - q, p + q

    def to_dict(self) -> dict:
        return {
            "alpha": self.alpha, "half_life": self.half_life, "gamma": self.gamma,
            "alpha_t": self.alpha_t, "residuals": self.residuals, "date_index": self.date_index,
        }

    @classmethod
    def from_dict(cls, blob: dict) -> DriftAwareConformal:
        return cls(**blob)
