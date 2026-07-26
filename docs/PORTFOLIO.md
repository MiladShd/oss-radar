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

> Built bounded repository automation that drains exact green daily-report PRs, validates one-file model-feature
> proposals, and consolidates duplicate drift incidents without granting direct-push bypass to automation.

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
