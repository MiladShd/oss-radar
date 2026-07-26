# Methodology

This document is deliberately candid about what the models do and don't claim. The goal of OSS Radar is a
**real, operated, explainable** system on real data — not a leaderboard-topping forecaster.

## Growth model (momentum)

**Task.** Predict 70-day smoothed download momentum:
`growth_target_70d = log1p(downloads[t+1..t+70]) - log1p(downloads[t-69..t])`.
The long horizon is intentional: raw 7-day download growth is mostly noise, while 10-week momentum is more
decision-useful and measurably learnable from the 180-day pypistats window.

**Features.** Pure download-dynamics computed from the daily series, so they are identically distributed between
historical training rows and the latest scoring row:

- `log_d7`, `log_d28`, `log_d56`, `log_d84` — weekly through 12-week download base (log1p)
- `velocity` — average daily downloads this week
- `mom_7v7` — this week vs. previous week
- `mom_7v28` — this week vs. the monthly average week
- `mom_28v28`, `mom_56v56`, `mom_28v56` — longer-horizon momentum ratios
- `trend_slope_28` — normalized least-squares slope over the last 28 days
- `trend_slope_56`, `trend_slope_84` — longer-horizon trend slopes
- `volatility_28` — coefficient of variation over the last 28 days

**Training data.** The first 180-day pypistats backfill yields hundreds of `(package, as-of-date)` rows (sliding
window, stride 3, ≥84 days of history before each as-of date; the captured 91-package production frame had 819).
Every later API window is upserted by package/date, so dates that fall out of the upstream rolling window remain in
`download_history` and the supervised set expands.

**Validation.** The operational split groups distinct as-of dates chronologically into 70% train, 15% validation,
and 15% untouched test partitions. Early stopping sees validation only; the frozen iteration count is then refit
on train+validation and scored once on the unclipped test outcomes. This prevents a single origin date from being
split row-wise, but it is **not independent temporal evidence**: the current 180-day source depth cannot place a
70-day embargo between partitions, so their outcome windows overlap. A separate package-disjoint gate measures
cross-sectional generalization. **Spearman rank correlation** is the promotion metric because the product use is
ranking packages by momentum, not claiming precise package-level forecasts.

**Honesty.** This is a ranked momentum watchlist, not a precise package-level forecast. The validation harness found
and removed a centered-moving-average leak; the defensible claim is cross-sectional ranking skill on 70-day momentum,
tracked openly with date-grouped Spearman and separate package-disjoint artifacts. `growth_pred_70d` is a
log-growth value; convert it to a conventional percent change with `100 * expm1(growth_pred_70d)`.
`momentum_score` is a bounded sigmoid of that log prediction (0 growth → 50), so it produces a stable ranking even
when absolute calibration is modest.

## Risk model

OSS Radar reports risk two ways, deliberately:

### 1. Risk score — composite plus an optional learned contribution

A documented weighted average of normalized sub-signals (higher = riskier):

| Component | Weight | Source |
|---|--:|---|
| recently published vulnerabilities (`vuln_new_28d` × `max_severity_new_28d`) | 0.24 | OSV.dev |
| release staleness (days since last release) | 0.20 | PyPI |
| maintainer key-person risk (`1 − bus_factor`) | 0.18 | ecosyste.ms DDS |
| weak security posture (`1 − scorecard/10`) | 0.16 | deps.dev / OpenSSF |
| abandoned / removed | 0.12 | ecosyste.ms / GitHub |
| issue backlog pressure (issues vs. merged PRs) | 0.10 | GitHub |

Missing inputs fall back to neutral priors rather than zero, so absence of data never looks like absence of risk.
The vulnerability component uses only advisories whose OSV `published` timestamp is within 28 days. Severity is
the worst normalized OSV database label or numeric/CVSS v2, v3, or v4 vector in that recent set. The lifetime
`max_severity` field remains available for audit context but cannot trigger a “recent” floor by itself.

The weighted result is subject to categorical safety floors:

| Explicit condition | Minimum score |
|---|--:|
| repository archived or ecosyste.ms status is non-empty | 85 |
| at least one recently published critical vulnerability | 75 |
| at least one recently published high-severity vulnerability | 66 |

The resulting value is persisted as `risk_composite_score`. When a compatible promoted classifier is available,
the dashboard's headline `risk_score` is:

```text
risk_score = 0.60 × risk_composite_score + 0.40 × (calibrated_risk_probability × 100)
```

The applicable safety floor is applied again **after** blending, so a low learned probability cannot wash an
archived/removed package or recent high/critical signal below its categorical minimum. If no compatible promoted
classifier exists, `risk_score` is the composite alone. The calibrated probability is persisted as
`risk_classifier_probability`; the API exposes both component values. Human-readable `risk_reasons` explain the
strongest composite drivers and name an active safety-floor condition; they do not claim to explain the
classifier's 40% contribution. Growth reasons are separate SHAP explanations.

The watchlist uses an OSV **package query without a version**, so these package-level vulnerability signals are not
proof that the latest installed version is affected. The dependency-audit path is different: when a requirement is
pinned, it sends that exact version to OSV and can describe active exposure for that version. Unpinned audit and
watchlist results remain package-history signals.

### 2. Risk model — LightGBM classifier (the learned view)

A classifier trained to predict a cross-sectional `at_risk_label`:

```
at_risk = (vuln_new_28d > 0 AND max_severity_new_28d in {HIGH, CRITICAL})
          OR archived OR (status is non-empty)
          OR (days_since_last_release > 365)
          OR (bus_factor < 0.1 AND dependent_repos > 1000)
```

from maintenance / popularity / security features. Once snapshot history spans 14 days, an eligible package-day
anchor is labeled only against the snapshot on its **exact +14-day outcome date**; anchors without that exact date
are skipped. Duplicate package-days are collapsed before labeling.

For the promotion metric, a stable SHA-256 partition reserves about 20% of package names as a holdout that the
served classifier never trains on. On the remaining packages, `StratifiedGroupKFold` produces package-disjoint
out-of-fold probabilities. A logistic Platt calibrator is fit only on those OOF probabilities; imputation is also
fit inside each fold. The final LightGBM model trains on the non-reserved packages, its probabilities pass through
the frozen calibrator, and calibrated `group_auc` plus Brier loss are measured once on the untouched reserved
cohort. If grouped OOF calibration cannot be formed, no learned risk artifact is eligible and scoring falls back to
the composite.

**Caveats (stated plainly):**

- The watchlist is small and mostly healthy → few positives → AUC is noisy on any single day. The model's job is to
  rank relative risk. It trains only when both the training and reserved holdout cohorts contain enough examples
  and at least three members of each class.
- The day-1 label is a heuristic rule. As daily `snapshots` accumulate, the pipeline automatically relabels
  against *realized forward outcomes* (did risk escalate?) and switches once there are enough rows.

## Champion / challenger

Each successfully trained candidate is recorded. For growth, the incumbent artifact is re-scored on the
candidate's exact current date-grouped test cohort before Spearman is compared. Risk uses the stable
reserved-package holdout and likewise re-scores a compatible incumbent on the current holdout. Comparisons are
allowed only inside the same versioned label/split lineage and exact benchmark hash; if a matching incumbent score
cannot be produced, the challenger is held rather than compared with an unrelated historical number. A candidate
must strictly beat that comparable incumbent, and growth must also clear its validation gate.

The risk promotion metric is calibrated `group_auc`, separate from the retired row-random AUC lineage. A first
champion in a new risk evaluation lineage must also meet the absolute bootstrap floor `group_auc >= 0.55`; the
absence of an incumbent is not permission to promote a below-chance model. Later candidates use the comparable
current-cohort incumbent policy above. Only compatible promoted learned artifacts are served; otherwise growth uses
a deterministic persistence baseline and risk uses the transparent composite alone. Either way metrics, benchmark
provenance, served version, and promotion rationale are persisted in BigQuery. Promoted model files are durable in
GCS. MLflow is a best-effort local `file:` trace and is ephemeral in Cloud Run, so it is not the production system
of record.

The full self-improvement design — forward-outcome relabeling, drift detection, and the automatic daily loop — is in
[IMPROVEMENT.md](IMPROVEMENT.md).

## Why agents, not an agentic model

The agents manage the *system*, they are not the predictor. This mirrors real MLOps: ingestion freshness checks,
data-quality gates, retraining + promotion, human-readable reporting, and PR/issue automation. The agent layer
degrades to deterministic templates whenever no LLM key is configured, so the pipeline never hard-fails on the LLM.
