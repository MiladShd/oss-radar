# Methodology

This document is deliberately candid about what the models do and don't claim. The goal of OSS Radar is a
**real, self-managing, explainable** system on real data — not a leaderboard-topping forecaster.

## Growth model (momentum)

**Task.** Predict next-7-day *relative* growth of weekly downloads:
`growth_target_7d = downloads[t+1..t+7] / downloads[t-6..t] − 1`.

**Features.** Pure download-dynamics computed from the daily series, so they are identically distributed between
historical training rows and the latest scoring row:

- `log_d7`, `log_d28` — weekly / monthly download base (log1p)
- `velocity` — average daily downloads this week
- `mom_7v7` — this week vs. previous week
- `mom_7v28` — this week vs. the monthly average week
- `trend_slope_28` — normalized least-squares slope over the last 28 days
- `volatility_28` — coefficient of variation over the last 28 days

**Training data.** The 180-day pypistats backfill yields thousands of `(package, as-of-date)` rows on the very
first run (sliding window, stride 3, ≥28 days of history before each as-of date).

**Validation.** A **time-aware split** (train on earlier as-of dates, test on later ones) — never a random shuffle —
so the reported MAE / RMSE / R² / Spearman reflect genuine forward forecasting. **Spearman rank correlation** is the
promotion metric because the product use is *ranking* packages by momentum, not predicting an exact number.

**Honesty.** 7-day download growth is noisy and partly driven by exogenous events (releases, CI mirrors, news).
Expect a modest positive Spearman that grows as more history accrues. `momentum_score` is a bounded sigmoid of the
prediction (0 growth → 50), so it always produces a sensible ranking even when absolute skill is low.

## Risk model

OSS Radar reports risk two ways, deliberately:

### 1. Risk score — transparent composite (the headline number)

A documented weighted average of normalized sub-signals (higher = riskier):

| Component | Weight | Source |
|---|--:|---|
| recent vulnerabilities (count × severity, 28-day) | 0.24 | OSV.dev |
| release staleness (days since last release) | 0.20 | PyPI |
| maintainer key-person risk (`1 − bus_factor`) | 0.18 | ecosyste.ms DDS |
| weak security posture (`1 − scorecard/10`) | 0.16 | deps.dev / OpenSSF |
| abandoned / removed | 0.12 | ecosyste.ms / GitHub |
| issue backlog pressure (issues vs. merged PRs) | 0.10 | GitHub |

Missing inputs fall back to neutral priors rather than zero, so absence of data never looks like absence of risk.
This score is fully explainable and is what the dashboard shows as the primary risk number.

### 2. Risk model — LightGBM classifier (the learned view)

A classifier trained to predict a cross-sectional `at_risk_label`:

```
at_risk = (recent HIGH/CRITICAL vuln) OR archived OR (status != null)
          OR (days_since_last_release > 365)
          OR (bus_factor < 0.1 AND dependent_repos > 1000)
```

from maintenance / popularity / security features. Reported with **cross-validated ROC-AUC**.

**Caveats (stated plainly):**

- The watchlist is small and mostly healthy → few positives → AUC is noisy on any single day. The model's job is to
  *rank* relative risk, and the classifier is only trained when there are ≥3 of each class.
- The day-1 label is a heuristic composite. As the daily `snapshots` history accumulates, the intended evolution is
  to relabel against *realized forward outcomes* (did risk escalate?) and let the classifier learn that instead.

## Champion / challenger

Each run trains fresh models and compares the primary metric (growth → Spearman, risk → AUC) to the best previous
**champion** recorded in the warehouse. A new model is promoted only if it **strictly beats** the champion (or there
is none). Either way every metric and the promotion rationale are persisted, so the dashboard's "model improvement
over time" chart is the honest audit trail — including runs that were *not* promoted.

The full self-improvement design — forward-outcome relabeling, drift detection, and the automatic daily loop — is in
[IMPROVEMENT.md](IMPROVEMENT.md).

## Why agents, not an agentic model

The agents manage the *system*, they are not the predictor. This mirrors real MLOps: ingestion freshness checks,
data-quality gates, retraining + promotion, human-readable reporting, and PR/issue automation. The agent layer
degrades to deterministic templates whenever no LLM key is configured, so the pipeline never hard-fails on the LLM.
