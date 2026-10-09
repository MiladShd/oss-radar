# OSS Radar portfolio notes

## What is demonstrably working

As of the captured production run on 2026-07-26:

- the scheduled Cloud Run job completed successfully on 91 packages and wrote 91 predictions;
- six public providers were surfaced as seven source-health checks, which reported 93–100% coverage with core
  downloads at 100%;
- BigQuery-backed run, model, prediction, drift, and agent-activity history is visible in the public dashboard;
- that historical build rejected its growth challenger through the generalization gate and retained its risk
  candidate, but its cross-run model comparisons predate the comparable-cohort fixes and are not presented as
  proof of current governance;
- the repository has DuckDB/BigQuery parity tests, CI, CodeQL, Terraform, immutable-image CD, deployment
  provenance, versioned remote state, exact Action pins, workflow-bound OIDC, zero-traffic dashboard verification,
  an isolated no-role pipeline smoke job, rollback, and bounded GitHub automation;
- the public API has parameterized package queries, short response caching, graceful no-data behavior, and an
  explicit first-run state;
- package discovery combines six stable primary categories, three curated cross-cutting capabilities, raw GitHub
  topics/language, and category-balanced limited demos without changing the production watchlist order.

The honest boundary is equally important: same-split scores have improved on some runs, but package-disjoint
metrics move in both directions, and the first 70-day production cohorts still need to mature. This repository
demonstrates ML/data/platform engineering and model governance; it does not claim continuous autonomous model
improvement.

## Corrected-model replay

A read-only replay on the 2026-07-26 production history exercised this release's corrected evaluation code without
writing models, registry rows, or warehouse data:

- growth: 819 rows across nine forecast origins; 637/91/91 date-grouped train/validation/test rows; test Spearman
  `0.849`, R² `0.545`, MAE `0.097`, and RMSE `0.244`;
- risk: 2,093 exact-14-day labeled rows; a stable 15-package, 345-row untouched holdout with zero package overlap;
  holdout AUC `0.827`, Brier `0.229`, and package-disjoint CV AUC `0.637`.

These results are useful implementation evidence, not a before/after improvement claim. Growth validation and
test each contain only one origin date and their 70-day outcome windows overlap; the corrected risk lineage is not
comparable to the retired row-random metric. Independent temporal growth evidence requires additional closed,
non-overlapping production cohorts.

## Reusable demo / LinkedIn blurb

> I built OSS Radar, a daily GCP data and ML product that tracks 91 Python/AI packages across six public providers,
> ranks adoption momentum, and surfaces dependency risk in a FastAPI dashboard. The repository combines
> DuckDB/BigQuery storage, causal LightGBM features, package-disjoint validation, model-promotion governance,
> drift/source-health monitoring, and an exact-SHA Terraform/GitHub Actions release path. The important engineering
> result is not “AI improves itself every day”; it is that candidates, failures, releases, and limitations are
> observable, bounded, and reproducible.

## Résumé bullets

> Built and deployed a daily GCP open-source intelligence platform tracking 91 Python/AI packages across six
> public providers and seven source-health checks; implemented causal LightGBM ranking, chronological
> date-grouped evaluation, a package-disjoint generalization gate, SHAP growth explanations, drift monitoring,
> BigQuery/DuckDB storage, and a cached FastAPI dashboard.

> Operationalized commit-to-production delivery with Terraform, keyless GitHub OIDC, least-privilege Cloud Run
> identities, versioned remote GCS state, deletion-blocking plans, a Terraform-managed immutable-tag Artifact
> Registry repository, exact-SHA Actions, workflow-bound OIDC, component-wise digest builds, a no-role DuckDB
> release smoke, zero-traffic verification, runtime provenance, and deterministic rollback.

> Calibrated dependency-risk probabilities with package-disjoint grouped OOF Platt scaling and an untouched stable
> holdout (read-only production-history replay: AUC `0.827`, 345 rows across 15 never-trained packages); added a
> `0.55` bootstrap AUC floor and post-blend safety minima for archived/removed packages and recent high/critical
> OSV/CVSS signals.

> Removed a centered-smoother lookahead bug, quantified shared-package inflation, and added a package-disjoint
> generalization gate; published unseen-package Spearman `0.683` versus a calibrated-persistence rank baseline
> `0.370`, with a separate package-block permutation test showing rank signal at `p < .001`.

> Diagnosed drifting forecast error (typical error 0.12 → 0.23 over four weeks of forecast dates) and showed that
> textbook split conformal prediction covered 59.6% of outcomes against an 80% target; added recency-weighted
> conformal quantiles and a clipped adaptive-width update, raising forward-chained coverage to
> 76.6% (88.4% at the 90% target) over seven scored dates, shipped 80% ranges to every package, and documented the
> limits (heuristic adaptation, small sample, lag-free evaluation) in [CONFORMAL.md](CONFORMAL.md).

> Added a survival-analysis view of dependency risk alongside the existing fixed-horizon classifier: a Cox
> proportional-hazards model with time-varying covariates, Breslow baseline hazard and package-clustered robust
> errors written in NumPy and verified against brute-force likelihood loops and an exact Poisson-regression
> identity (coefficients agree to 1e-14). Temporal landmark forecasts against recency-matched baselines showed
> that anchoring probabilities to the recent event rate, not the survival form alone, removed a 2 to 4x
> over-forecast (17 to 29% predicted vs 8% observed); the survival model then had the lowest Brier score in every
> window (0.054 to 0.061 vs 0.059 to 0.122 for an anchored classifier), while advisory history alone
> ranked slightly better (AUC 0.85 vs 0.82 to 0.84).

> Built a 219-test automated suite (23 headless-Chromium Playwright tests plus API, statistical, and pipeline
> tests) that drives the live dashboard against a seeded warehouse: every tab, search/sort/category filters, the
> package drawer, the dependency-audit form, conformal intervals reaching the UI, graceful no-interval states, and
> a 375px mobile layout; wired into GitHub Actions so regressions block merges.

> Published the dashboard on a custom domain behind a Cloudflare Worker reverse proxy with 5-minute edge caching
> (cache hits measured around 130 ms from a laptop, versus 0.2–0.3 s uncached), method allow-listing, and uncached
> health and audit paths, in front of a Cloud Run service capped at two instances.

## Interview defense

- **Why split by forecast-origin date?** Rows sharing one as-of date have overlapping information, so their entire
  date group stays in one chronological partition. Package-disjoint evaluation separately measures cross-sectional
  generalization, and the current short history is not presented as independent time-forward evidence.
- **Why champion/challenger?** A candidate should not replace a working artifact merely because it trained later.
  The incumbent is re-scored on the candidate's exact compatible cohort; incomparable or losing candidates are
  retained as evidence but not served.
- **Why deterministic operational roles instead of an autonomous agent?** Ingestion checks, promotion gates, and
  repository actions need repeatable inputs, bounded permissions, and auditable outcomes. Claude may rewrite brief
  prose, but it does not transform data, train the models, or make predictions.
- **Why no Spark, Airflow, or dbt?** Ninety-one packages, one daily job, and a small portable transformation path fit
  pandas plus Cloud Scheduler/Cloud Run. Add distributed compute, DAG orchestration, or warehouse-native modeling
  only when measured data volume, dependency/backfill complexity, or shared SQL ownership requires it; see
  [ARCHITECTURE.md](ARCHITECTURE.md#deliberate-stack-tradeoffs).
- **How is the public dashboard defended?** Package queries use strict name validation and warehouse parameters;
  read responses have a short process-local cache; live audits have body/rate limits; Cloud Run has a bounded
  instance count; and health, first-run, and error states avoid treating missing data as success.

## Interview-safe claims

Say:

- “The system runs daily and records every successfully trained candidate, promotion decision, drift signal, and
  served model version; the operationalized release also records the full deployment SHA.”
- “Champion/challenger re-scores the incumbent on the candidate's exact current cohort and prevents a losing or
  non-comparable candidate from replacing the selected artifact.”
- “The risk classifier is OOF-calibrated by package, while explicit safety conditions remain policy floors after
  blending; package-level watchlist CVEs are not misrepresented as version-confirmed exposure.”
- “The package-disjoint result demonstrates cross-sectional ranking skill.”
- “Forward temporal quality will be evaluated when the 70-day production cohorts close.”

Avoid:

- “The model gets better every day.”
- “Champion/challenger guarantees future real-world quality.”
- “The project is fully autonomous.”
- “A high same-split metric proves forecasting skill on unseen packages or future regimes.”

The strongest portfolio story is not a perfect model score. It is the combination of candid validation,
production operations, failure containment, and evidence-backed limits.
- **Why not trust the standard conformal guarantee?** It requires exchangeability, and this series drifts: typical
  error roughly doubled over a month. The first implementation under-covered (59.6% vs 80%), so I measured the drift,
  added recency weighting and an adaptive-width update, and evaluated by forward-chaining so no date is scored
  with later information. The update is a clipped, batched heuristic, not the published algorithm, so I report
  measured coverage (76.6% vs 80%) and its limits: seven scored dates, overlapping 70-day outcome windows, a
  lag-free evaluation, and settings compared on the same dates. An independent review also found that my
  calibration model had been early-stopped on the same validation dates it was then scored on; I fixed it,
  re-ran everything, and added a regression test.
- **How do you know the website works?** Unit and statistical tests cover the math; API tests cover the JSON
  contracts; Playwright browser tests cover what a visitor does. In CI the browser tests are required to run (a
  missing browser fails the build) and locally they skip when the CDN or Chromium is unavailable.
- **Why survival analysis instead of a classifier?** A classifier needs a fixed horizon and discards timing and
  partial follow-up; the Cox model uses every day of exposure and handles censoring. But the evidence was more
  specific than that: the *calibration* improvement came from anchoring to the recent event rate, which I could
  also give the classifier. With that anchoring the survival model still had the lowest Brier score in every
  window, by modest margins on small samples, and a constant recent rate was a strong baseline.
- **What did the survival model teach you that you did not expect?** Days since last release had a hazard ratio of
  about 0.75, so recently released packages show more advisory-count increases, the opposite of treating
  staleness as risk. A scrutiny explanation is plausible but untested; the model measures advisory arrival, not
  exploitability.
- **What did the review catch?** A live open-proxy bug in my Cloudflare Worker (`//host/path` escaped the
  configured origin), the early-stopping leak above, and event labels that would have counted a failed OSV
  lookup as a burst of advisories. I verified each, fixed it, and added regression tests.
