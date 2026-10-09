"""Survival analysis for dependency risk: time until a package receives a *new* security advisory.

The classifier in ``risk.py`` answers "will something bad happen within a fixed 14 days?" with a 0/1 label,
which discards *when* it happens and treats a package observed for 14 days exactly like one observed for
110. A survival model uses every day of follow-up and handles censoring (no event yet) correctly.

Model: Cox proportional hazards with time-varying covariates, fitted from daily snapshots.

    λᵢ(t) = λ₀(t) · exp(xᵢ(t−1)ᵀ β)

``t`` is calendar time (days since the first snapshot), so the baseline hazard λ₀(t) absorbs shocks common
to all packages (e.g. an OSV bulk ingest that raised 20+ packages on one day) instead of attributing them to
covariates. Covariates are measured the day *before* the interval they predict, so nothing leaks from the
outcome day. Recurrent events are allowed (Andersen–Gill counting process), with package-clustered robust
standard errors. Everything here is NumPy only; ``docs/SURVIVAL.md`` derives the likelihood.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# Time-varying covariates, each transformed to be roughly symmetric and then standardised at fit time.
SURVIVAL_FEATURES = [
    "log_vuln_history",    # log1p(cumulative advisories already known)
    "log_vuln_recent_28d",  # log1p(advisories published in the last 28 days)
    "log_downloads",       # log1p(monthly downloads): popularity draws scrutiny
    "log_days_since_release",
    "scorecard",           # OpenSSF scorecard / 10
    "bus_factor",
]


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    """A numeric column, or all-NaN when the snapshot frame lacks it (older warehouses)."""
    if column not in frame:
        return pd.Series(np.nan, index=frame.index)
    return pd.to_numeric(frame[column], errors="coerce")


def covariate_frame(snapshots: pd.DataFrame) -> pd.DataFrame:
    """Transform raw snapshot columns into the survival covariates (missing values left as NaN)."""
    s = snapshots
    out = pd.DataFrame(index=s.index)
    out["log_vuln_history"] = np.log1p(_numeric(s, "vuln_count").clip(lower=0))
    out["log_vuln_recent_28d"] = np.log1p(_numeric(s, "vuln_new_28d").clip(lower=0))
    out["log_downloads"] = np.log1p(_numeric(s, "monthly_downloads").clip(lower=0))
    out["log_days_since_release"] = np.log1p(_numeric(s, "days_since_last_release").clip(lower=0))
    out["scorecard"] = _numeric(s, "scorecard_overall") / 10.0
    out["bus_factor"] = _numeric(s, "bus_factor")
    return out


def _osv_valid(frame: pd.DataFrame) -> pd.Series:
    """True where the snapshot's OSV lookup succeeded.

    ``ingest.osv.fetch`` returns ``vuln_count=0`` when the request fails, so a failed day looks like
    "zero advisories" and the recovery looks like a burst of new ones. The per-source health flag is
    stored with each snapshot; rows without it (older warehouses) are treated as valid.
    """
    if "source_status" not in frame:
        return pd.Series(True, index=frame.index)

    def ok(value) -> bool:
        if isinstance(value, dict):
            return bool(value.get("osv", True))
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except ValueError:
                return True
            return bool(parsed.get("osv", True)) if isinstance(parsed, dict) else True
        return True

    return frame["source_status"].map(ok).astype(bool)


def build_counting_process(snapshot_history: pd.DataFrame) -> pd.DataFrame:
    """One row per package-day: covariates at day t−1, ``event`` = a new advisory appeared on day t.

    ``day`` is days since the earliest snapshot (the first interval is day 1). A package-day whose
    previous snapshot is missing is dropped rather than imputed. Returns an empty frame if there is not
    enough history.
    """
    if snapshot_history.empty or "snapshot_date" not in snapshot_history:
        return pd.DataFrame()
    df = snapshot_history.copy()
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["snapshot_date", "name"])
    if "ingested_at" in df:
        df["ingested_at"] = pd.to_datetime(df["ingested_at"], errors="coerce")
        df = df.sort_values(["name", "snapshot_date", "ingested_at"])
    else:
        df = df.sort_values(["name", "snapshot_date"])
    df = df.drop_duplicates(["name", "snapshot_date"], keep="last").reset_index(drop=True)
    origin = df["snapshot_date"].min()
    df["day"] = (df["snapshot_date"] - origin).dt.days
    count = _numeric(df, "vuln_count")
    # A count is only usable if OSV answered and the value is finite; otherwise it is *unobserved*.
    df["_vc"] = count.where(_osv_valid(df) & np.isfinite(count))
    cov = covariate_frame(df)
    frame = pd.concat([df[["name", "day", "_vc"]], cov], axis=1)
    by = frame.groupby("name", sort=False)
    prev_day = by["day"].shift(1)
    prev_vc = by["_vc"].shift(1)
    out = frame.copy()
    for col in SURVIVAL_FEATURES:
        out[col] = by[col].shift(1)  # covariates as of the previous snapshot
    # An interval is observed only if both endpoints are consecutive days with a valid count. Anything
    # else is unobserved follow-up and is dropped, never labelled "no event".
    observed = ((out["day"] - prev_day) == 1) & out["_vc"].notna() & prev_vc.notna()
    out["event"] = (out["_vc"] > prev_vc).astype(int)
    out = out[observed].drop(columns=["_vc"]).reset_index(drop=True)
    return out


@dataclass
class CoxFit:
    """Fitted Cox model: coefficients on standardised covariates, robust SEs, baseline hazard."""

    features: list[str]
    beta: np.ndarray
    se_robust: np.ndarray
    se_model: np.ndarray
    log_likelihood: float
    null_log_likelihood: float
    n_rows: int
    n_events: int
    n_event_days: int
    mean: np.ndarray
    scale: np.ndarray
    fill: np.ndarray
    baseline_hazard: dict[int, float]  # Breslow: day -> d_t / S0_t
    l2: float
    converged: bool
    log_likelihood_unpenalized: float = float("nan")

    @property
    def hazard_ratios(self) -> np.ndarray:
        return np.exp(self.beta)

    def linear_predictor(self, X: pd.DataFrame) -> np.ndarray:
        Z = (X[self.features].astype(float).fillna(dict(zip(self.features, self.fill, strict=True)))
             .to_numpy() - self.mean) / self.scale
        return Z @ self.beta

    def summary(self) -> pd.DataFrame:
        z = self.beta / self.se_robust
        return pd.DataFrame({
            "feature": self.features,
            "coef_per_sd": self.beta,
            "hazard_ratio_per_sd": np.exp(self.beta),
            "hr_ci_low": np.exp(self.beta - 1.96 * self.se_robust),
            "hr_ci_high": np.exp(self.beta + 1.96 * self.se_robust),
            "robust_se": self.se_robust,
            "z": z,
            "p_value": [math.erfc(abs(v) / math.sqrt(2)) for v in z],
        })

    def recent_baseline(self, window_days: int = 28) -> float:
        """Mean daily baseline hazard over the latest ``window_days`` observed days (0 if none)."""
        if not self.baseline_hazard:
            return 0.0
        last = max(self.baseline_hazard)
        days = range(last - window_days + 1, last + 1)
        return float(np.mean([self.baseline_hazard.get(d, 0.0) for d in days]))

    def event_probability(self, X: pd.DataFrame, horizon_days: int, window_days: int = 28) -> np.ndarray:
        """P(at least one new advisory within ``horizon_days``) = 1 − exp(−h̄₀ · H · exp(xβ)).

        Assumes covariates stay at today's values and the baseline hazard stays at its recent level,
        so this is a forecast conditional on the current regime, not a guarantee.
        """
        eta = self.linear_predictor(X)
        return 1.0 - np.exp(-self.recent_baseline(window_days) * horizon_days * np.exp(eta))


def _risk_set_sums(Z: np.ndarray, w: np.ndarray, day_idx: np.ndarray, n_days: int):
    """Per-day S0=Σw, S1=Σ w·z, S2=Σ w·z zᵀ over the risk set (all rows of that day)."""
    p = Z.shape[1]
    S0 = np.bincount(day_idx, weights=w, minlength=n_days)
    S1 = np.zeros((n_days, p))
    S2 = np.zeros((n_days, p, p))
    for a in range(p):
        S1[:, a] = np.bincount(day_idx, weights=w * Z[:, a], minlength=n_days)
        for b in range(a, p):
            S2[:, a, b] = S2[:, b, a] = np.bincount(day_idx, weights=w * Z[:, a] * Z[:, b], minlength=n_days)
    return S0, S1, S2


def fit_cox(
    frame: pd.DataFrame,
    features: list[str] | None = None,
    *,
    l2: float = 1e-3,
    max_iter: int = 50,
    tol: float = 1e-9,
) -> CoxFit:
    """Maximise the Breslow partial likelihood by Newton–Raphson (with a small ridge for stability).

    Log partial likelihood, with D_t the packages having an event on day t and R_t all packages at risk:

        ℓ(β) = Σ_t [ Σ_{i∈D_t} x_iᵀβ − d_t · log Σ_{j∈R_t} exp(x_jᵀβ) ]

    Gradient  U(β) = Σ_t [ Σ_{i∈D_t} x_i − d_t · S1_t/S0_t ]
    Hessian   H(β) = −Σ_t d_t [ S2_t/S0_t − (S1_t/S0_t)(S1_t/S0_t)ᵀ ]
    """
    features = list(features or SURVIVAL_FEATURES)
    data = frame.dropna(subset=["day", "event"]).copy()
    fill = data[features].median().fillna(0.0).to_numpy(dtype=float)
    X = data[features].astype(float).fillna(dict(zip(features, fill, strict=True))).to_numpy()
    mean = X.mean(axis=0)
    scale = X.std(axis=0)
    scale[scale == 0] = 1.0
    Z = (X - mean) / scale
    event = data["event"].to_numpy(dtype=float)
    days = data["day"].to_numpy(dtype=int)
    uniq, day_idx = np.unique(days, return_inverse=True)
    n_days, p = len(uniq), len(features)
    d_t = np.bincount(day_idx, weights=event, minlength=n_days)
    sum_x_events = np.zeros(p)
    for a in range(p):
        sum_x_events[a] = (np.bincount(day_idx, weights=event * Z[:, a], minlength=n_days)).sum()
    event_days = d_t > 0

    def objective(beta: np.ndarray):
        eta = Z @ beta
        w = np.exp(eta - eta.max())  # stabilised; the shift cancels in every S1/S0 and log-ratio
        S0, S1, S2 = _risk_set_sums(Z, w, day_idx, n_days)
        shift = eta.max()
        ll = float(sum_x_events @ beta - np.sum(d_t[event_days] * (np.log(S0[event_days]) + shift)))
        xbar = S1[event_days] / S0[event_days, None]
        grad = sum_x_events - (d_t[event_days, None] * xbar).sum(axis=0)
        cov_t = S2[event_days] / S0[event_days, None, None] - xbar[:, :, None] * xbar[:, None, :]
        hess = -(d_t[event_days, None, None] * cov_t).sum(axis=0)
        return ll - 0.5 * l2 * beta @ beta, grad - l2 * beta, hess - l2 * np.eye(p), (S0, S1, shift)

    beta = np.zeros(p)
    ll0, *_ = objective(beta)
    ll, grad, hess, _ = objective(beta)
    converged = False
    for _ in range(max_iter):
        step = np.linalg.solve(hess, -grad)
        t = 1.0
        while t > 1e-8:  # backtracking line search keeps the likelihood monotone
            cand = beta + t * step
            cand_ll, *_ = objective(cand)
            if cand_ll >= ll - 1e-12:
                break
            t *= 0.5
        beta = cand
        new_ll, grad, hess, _ = objective(beta)
        if abs(new_ll - ll) < tol and np.max(np.abs(grad)) < 1e-6:
            ll, converged = new_ll, True
            break
        ll = new_ll

    ll, grad, hess, (S0, S1, shift) = objective(beta)
    info = -hess
    cov_model = np.linalg.inv(info)

    # Robust (sandwich) covariance, clustered by package: score residual per row is
    #   r_it = (z_it − z̄_t) · (δ_it − exp(η_it) · d_t / S0_t)
    eta = Z @ beta
    w = np.exp(eta - shift)
    with np.errstate(divide="ignore", invalid="ignore"):
        zbar = np.where(S0[:, None] > 0, S1 / S0[:, None], 0.0)
        cum_inc = np.where(S0 > 0, d_t / S0, 0.0)  # Breslow increment in the shifted scale
    resid = (Z - zbar[day_idx]) * (event - w * cum_inc[day_idx])[:, None]
    cluster = pd.factorize(data["name"])[0]
    U = np.zeros((cluster.max() + 1, p))
    np.add.at(U, cluster, resid)
    cov_robust = cov_model @ (U.T @ U) @ cov_model

    baseline = {int(day): float(inc * math.exp(-shift)) for day, inc in zip(uniq, cum_inc, strict=True)}
    return CoxFit(
        features=features, beta=beta,
        se_robust=np.sqrt(np.clip(np.diag(cov_robust), 0, None)),
        se_model=np.sqrt(np.clip(np.diag(cov_model), 0, None)),
        log_likelihood=ll, null_log_likelihood=ll0,
        n_rows=len(data), n_events=int(event.sum()), n_event_days=int(event_days.sum()),
        mean=mean, scale=scale, fill=fill, baseline_hazard=baseline, l2=l2, converged=converged,
        log_likelihood_unpenalized=float(ll + 0.5 * l2 * beta @ beta),
    )


def fit_cox_selected(
    frame: pd.DataFrame,
    features: list[str] | None = None,
    *,
    z_threshold: float = 2.0,
    **kwargs,
) -> CoxFit:
    """Fit all covariates, keep those with robust |z| > ``z_threshold``, refit on the survivors.

    The rule uses only the rows it is given, so applying it inside a training window cannot leak test
    information. At least the single strongest covariate is always kept.
    """
    full = fit_cox(frame, features, **kwargs)
    z = np.abs(full.beta / np.where(full.se_robust > 0, full.se_robust, np.inf))
    keep = [f for f, zi in zip(full.features, z, strict=True) if zi > z_threshold]
    if not keep:
        keep = [full.features[int(np.argmax(z))]]
    return full if len(keep) == len(full.features) else fit_cox(frame, keep, **kwargs)


MIN_EVENTS_TO_SCORE = 30
MIN_DAYS_TO_SCORE = 28


def current_covariates(snapshot_history: pd.DataFrame) -> pd.DataFrame:
    """Covariates from each package's most recent snapshot (what a forecast made today conditions on)."""
    df = snapshot_history.copy()
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"], errors="coerce")
    df = df.dropna(subset=["snapshot_date", "name"])
    if "ingested_at" in df:
        df = df.sort_values(["name", "snapshot_date", "ingested_at"])
    else:
        df = df.sort_values(["name", "snapshot_date"])
    latest = df.drop_duplicates("name", keep="last")
    out = covariate_frame(latest)
    out.insert(0, "name", latest["name"].to_numpy())
    return out.reset_index(drop=True)


def score_new_advisory_risk(
    snapshot_history: pd.DataFrame,
    horizons: tuple[int, ...] = (14, 30),
) -> tuple[pd.DataFrame, dict] | None:
    """Probability each package gets a new advisory within each horizon, from the daily snapshots.

    Returns ``(scores, info)`` or None when the history is too thin to trust (fewer than
    ``MIN_EVENTS_TO_SCORE`` events or ``MIN_DAYS_TO_SCORE`` days), so the dashboard shows nothing
    rather than a number built on a handful of events.
    """
    frame = build_counting_process(snapshot_history)
    if frame.empty or frame["day"].max() < MIN_DAYS_TO_SCORE or int(frame["event"].sum()) < MIN_EVENTS_TO_SCORE:
        return None
    fit = fit_cox_selected(frame)
    if not fit.converged:
        return None
    now = current_covariates(snapshot_history)
    scores = pd.DataFrame({"name": now["name"]})
    for horizon in horizons:
        scores[f"p_new_advisory_{horizon}d"] = np.round(fit.event_probability(now, horizon), 4)
    info = {
        "features": fit.features,
        "n_events": fit.n_events,
        "n_rows": fit.n_rows,
        "n_packages": int(frame["name"].nunique()),
        "n_days": int(frame["day"].max()),
        "recent_baseline_daily_hazard": fit.recent_baseline(),
        "hazard_ratios_per_sd": {
            f: float(hr) for f, hr in zip(fit.features, fit.hazard_ratios, strict=True)
        },
    }
    return scores, info


@dataclass
class LandmarkResult:
    horizon_days: int
    n_landmarks: int
    n_rows: int
    n_positive: int
    auc: float
    brier: float
    mean_predicted: float
    observed_rate: float
    per_landmark_auc: list[float] = field(default_factory=list)


def _auc(y: np.ndarray, score: np.ndarray) -> float:
    """Mann–Whitney AUC with tie handling; NaN if only one class."""
    y = np.asarray(y, dtype=int)
    n1, n0 = int(y.sum()), int((1 - y).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    ranks = pd.Series(score).rank(method="average").to_numpy()
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def landmark_evaluation(
    frame: pd.DataFrame,
    fit: CoxFit,
    scorer,
    *,
    first_landmark: int,
    horizon_days: int,
    step_days: int = 7,
) -> LandmarkResult:
    """Honest forecast check: at each landmark day L, predict P(new advisory in (L, L+H]) for every
    package from covariates known at L, then compare with what happened. ``scorer(rows)`` maps the
    landmark rows to probabilities (or any monotone score for ranking-only comparators)."""
    last_day = int(frame["day"].max())
    ys, ps, aucs = [], [], []
    landmarks = list(range(first_landmark, last_day - horizon_days + 1, step_days))
    for L in landmarks:
        at = frame[frame["day"] == L + 1]  # row for day L+1 holds covariates measured on day L
        if at.empty:
            continue
        window = frame[(frame["day"] >= L + 1) & (frame["day"] <= L + horizon_days)]
        followed = window.groupby("name").size()
        complete = set(followed[followed == horizon_days].index)
        at = at[at["name"].isin(complete)]  # a package not observed for the full window has no label
        if at.empty:
            continue
        future = window[window["day"] > L + 1]
        had = set(future.loc[future["event"] == 1, "name"])
        y = at["name"].isin(had).astype(int).to_numpy()
        # outcome window is days L+1 … L+H: day L+1 is the covariate row itself, L+2 … L+H follow it
        had_first = set(at.loc[at["event"] == 1, "name"])
        y = np.maximum(y, at["name"].isin(had_first).astype(int).to_numpy())
        p = np.asarray(scorer(at), dtype=float)
        ys.append(y)
        ps.append(p)
        aucs.append(_auc(y, p))
    if not ys:
        return LandmarkResult(horizon_days, 0, 0, 0, float("nan"), float("nan"), float("nan"), float("nan"))
    y_all, p_all = np.concatenate(ys), np.concatenate(ps)
    return LandmarkResult(
        horizon_days=horizon_days, n_landmarks=len(ys), n_rows=int(len(y_all)), n_positive=int(y_all.sum()),
        auc=_auc(y_all, p_all),
        brier=float(np.mean((p_all - y_all) ** 2)) if np.all((p_all >= 0) & (p_all <= 1)) else float("nan"),
        mean_predicted=float(np.mean(p_all)), observed_rate=float(np.mean(y_all)),
        per_landmark_auc=[float(a) for a in aucs],
    )
