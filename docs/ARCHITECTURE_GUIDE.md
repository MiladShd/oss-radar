# OSS Radar — architecture and interview guide

This guide describes the operationalized codebase. Historical validation numbers are labeled as
historical evidence; moving-cohort metrics are not presented as proof that the model improves over
time.

## 1. The 60-second pitch

OSS Radar is a daily open-source intelligence pipeline for a curated watchlist of Python/AI
packages. It collects download, maintenance, ecosystem, dependency, and vulnerability signals;
builds causal download features; trains LightGBM growth and risk candidates; ranks packages with
scoped explanations; and publishes the results through a FastAPI dashboard.

The engineering story is broader than the models:

- the same application code uses DuckDB locally/CI and BigQuery in production;
- evaluation cohorts and serving versions are recorded instead of assuming retraining means
  improvement;
- promoted model artifacts are durable in GCS and operational decisions are durable in BigQuery;
- seven deterministic operational roles execute eight bounded steps, with Claude optional only for
  report prose;
- Terraform defines a least-privilege GCP stack, while keyless CD deploys verified image digests;
- GitHub automation is limited to exact, owner-authored, green PR and issue shapes.

The defensible model claim is **cross-sectional ranking skill**, not independent 70-day temporal
forecasting. The current date-grouped growth split has overlapping outcome windows because a
70-day embargo is impossible with the available source depth.

## 2. Scope, sources, and taxonomy

The watchlist currently contains 91 packages. Production uses **six public providers surfaced as
seven source-health checks**:

| Provider | Signal | Health-check detail |
|---|---|---|
| pypistats | daily download history | one check; the sole source used for growth history |
| PyPI JSON | releases, versions, repository URL | one check |
| deps.dev | dependency graph and OpenSSF Scorecard | one check |
| OSV.dev | known vulnerabilities and normalized OSV/CVSS severity | one check |
| ecosyste.ms | package metadata and repository health | two checks |
| GitHub REST | commit, PR, and issue activity | one check; token optional |

The sources degrade independently, but pypistats is intentionally load-bearing for growth training:
without daily downloads there is no new growth frame.

The production watchlist queries OSV by package, not installed version. Its recent field is
`max_severity_new_28d`: the worst database label or numeric/CVSS v2/v3/v4 vector among advisories
published in the last 28 days. It is a package-history signal, not proof the latest version is
affected. The dependency auditor sends an exact version for pinned requirements.

Packages have two levels of classification:

1. one of six stable, mutually exclusive primary categories;
2. zero or more curated cross-cutting capabilities:
   `inference_serving_runtime`, `evaluation_observability`, and
   `workflow_orchestration`.

Raw GitHub topics and primary language are retained as upstream metadata rather than forced into the
curated taxonomy. Positive watchlist limits use deterministic category round-robin sampling and
prefer unique repositories, so small demos are balanced. Full production runs preserve the curated
watchlist order.

## 3. One daily run

Cloud Scheduler invokes one Cloud Run Job. `run_pipeline` records one UTC `run_id` and six timed
stages:

1. **Ingest** — collect package snapshots and daily download history; retry only packages whose
   core `downloads_7d` signal failed; upsert history on `(name, date)`.
2. **Features** — build the growth training/scoring frames and choose heuristic or exact-horizon
   risk labels.
3. **Train** — fit growth and risk candidates and compute evaluation provenance.
4. **Score** — select compatible promoted artifacts or deterministic fallbacks, then create
   predictions.
5. **Self-audit** — audit this repository's own dependencies.
6. **Agents** — execute eight bounded steps across seven recorded roles and create the report/PR
   when configured.

Writes are interleaved rather than wrapped in one distributed transaction. A failed process can
leave completed upstream writes without a final successful `pipeline_runs` row. Natural-key
upserts make download-history replays idempotent, and the next run can recover, but a
`try/finally` failed-run record and Cloud Monitoring alert remain sensible hardening work.

## 4. Ingestion and warehouse contracts

Connectors return flat dictionaries with source-status flags and missing values instead of raising
for expected remote failures. The shared HTTP layer applies per-host throttling and bounded retry
with backoff.

The Healer is narrower than “repair every source”:

- it detects only snapshots missing the core `downloads_7d` signal;
- it retries each affected package once, single-threaded;
- if the retry still fails, it may carry forward that package's last good snapshot.

An auxiliary connector failure degrades only its fields and appears in source coverage; it does
not cause whole-snapshot carry-forward.

One schema definition maps into DuckDB and BigQuery. JSON-shaped values are serialized for
portability. `download_history` is retained with a natural-key upsert instead of truncation, so
older dates remain available after they fall outside an upstream rolling API window.

Durability is explicit:

- **BigQuery:** snapshots, features, predictions, candidate metrics, comparison provenance, served
  versions, agent activity, and pipeline runs;
- **GCS:** promoted/held serialized model artifacts and validation artifacts;
- **MLflow:** best-effort local `file:` tracking. In Cloud Run its filesystem is ephemeral, so it is
  not the production system of record.

## 5. Growth model and evaluation

### Target and features

The target is 70-day log momentum:

```text
growth_target_70d =
  log1p(downloads[t+1..t+70]) - log1p(downloads[t-69..t])
```

Features use only information available at the as-of date: download levels, momentum ratios,
causal trend slopes, velocity, and volatility. The initial 180-day backfill produces **hundreds**
of supervised package/date rows (819 in the captured 91-package frame), not thousands. Accumulated
`download_history` expands that frame over time.

`growth_pred_70d` is log-growth. A conventional percent interpretation must use:

```text
percent change = 100 × expm1(growth_pred_70d)
```

Formatting the raw log value directly as a percentage is incorrect.

### Operational split

Distinct feature dates are split chronologically 70%/15%/15%:

- train dates fit a tuning model;
- validation dates are the only early-stopping evaluation;
- the chosen iteration count is frozen;
- train+validation are refit;
- the final model is scored once against untouched, **unclipped** test outcomes.

This prevents rows from one origin date crossing partitions. It does **not** create independent
temporal evidence: the source window cannot support a 70-day embargo, and outcome windows overlap
between partitions. The provenance record states `embargo_days: 0` and
`independent_temporal_evidence: false`.

The growth validation gate separately checks known leak signatures and package-disjoint
generalization. Historical validation work found:

- centered-smoother lookahead inflated the metric;
- shared packages inflated same-universe evaluation;
- a historical package-disjoint harness produced R² `0.363` and Spearman `0.683`;
- a calibrated-persistence rank baseline produced Spearman `0.370`;
- a package-block permutation test found rank signal at `p < .001`.

Those are historical cross-sectional results, not a promise about future temporal cohorts.

## 6. Risk model, score, and explanations

### Labels and holdout

The cold-start label is a transparent heuristic. Once enough snapshot history exists, the pipeline
builds realized-outcome labels at an exact horizon:

- pair an anchor only with a snapshot on exactly `anchor date + 14 days`;
- skip anchors without that exact outcome date;
- collapse duplicate package/day snapshots before pairing;
- switch only when enough rows and both classes exist.

A stable SHA-256 rule reserves about 20% of package names. The served risk classifier never trains
on those packages. On the remaining packages, `StratifiedGroupKFold` produces package-disjoint OOF
probabilities with fold-local imputation. A logistic Platt calibrator is fit on those OOF
probabilities, then frozen. The final LightGBM model trains on all non-reserved packages and its
calibrated `group_auc` and Brier loss are measured on the untouched reserved cohort. If grouped OOF
calibration is unavailable, the candidate is ineligible and scoring remains composite-only.

### Headline score

The transparent composite weights are:

| Component | Weight |
|---|--:|
| recently published vulnerabilities (`vuln_new_28d` × `max_severity_new_28d`) | 0.24 |
| release staleness | 0.20 |
| maintainer key-person risk | 0.18 |
| weak security posture | 0.16 |
| abandoned/removed | 0.12 |
| issue backlog pressure | 0.10 |

When a compatible promoted classifier exists:

```text
risk_score = 0.60 × composite + 0.40 × calibrated_probability × 100
```

Categorical policy floors are applied to the composite and again after blending: archived or
non-empty removal status → `85`, a recently published critical vulnerability → `75`, and a recently
published high-severity vulnerability → `66`. A low calibrated probability therefore cannot dilute
an explicit hazard below its minimum. Without a classifier, risk is composite-only. The API exposes
`risk_composite_score` and the calibrated `risk_classifier_probability` separately. `risk_reasons` explain
composite/floor drivers; they do not explain the classifier's 40% contribution. Growth reasons are
SHAP-derived and stored separately.

## 7. Cohort-aware champion/challenger

Every successfully trained candidate records:

- label and split versions;
- dataset and benchmark hashes;
- feature-set hash;
- candidate metric;
- incumbent comparison metric/version/mode;
- promotion note and the artifact actually served.

For growth, the incumbent artifact is re-scored on the candidate's exact current date-grouped test
cohort. For risk, a compatible incumbent is re-scored on the candidate's exact current reserved
package cohort. A comparison is valid only inside the same versioned evaluation lineage and
benchmark hash. If no matching incumbent evaluation can be produced, the challenger is held; it is
not compared with an unrelated “best-ever” number.

Growth also has to pass its leakage/generalization gate. That gate does not apply to risk; risk is
constrained by the stable package-disjoint evaluation lineage. The first champion in a new risk
lineage must clear an absolute calibrated holdout AUC floor of `0.55`; no-incumbent bootstrap does
not promote a below-floor candidate.

Serving follows promotion:

- promoted compatible candidate;
- otherwise a compatible previous champion;
- otherwise deterministic growth persistence or composite-only risk.

A held risk classifier is never mixed into scoring. A cloud candidate whose artifact cannot be
persisted to GCS cannot become champion.

## 8. Operational roles and bounded automation

Seven unique roles produce activity rows; DataScientist executes both training-summary and drift
steps, giving eight total steps:

1. Healer;
2. DataEngineer;
3. DataQuality;
4. DataScientist — training/promotion;
5. DataScientist — drift monitoring;
6. ImprovementScientist;
7. RiskAnalyst;
8. MLOps.

The roles are deterministic controllers. Claude is optional and may rewrite only the daily brief;
it never produces scores. Feature proposals come from a fixed implemented catalog, edit one JSON
configuration file, and still require exact-diff validation and green CI.

Drift compares the current and prior score distributions with PSI plus label churn. High drift
records a review recommendation and opens/comments on a narrowly matched GitHub issue. It does not
set a hidden “force retrain” flag—the pipeline already retrains eligible candidates each run.

Auto-triage similarly checks exact owner, branch, title, labels, files, head commit, and required
checks before merging. Drift deduplication touches only exact owner-authored automation issues and
preserves the oldest canonical thread.

## 9. Dashboard and public API

The FastAPI service exposes overview, package, model, run, agent, audit, self-audit, and health
views without a frontend build toolchain.

Operational guardrails include:

- parameterized package-detail queries through the shared warehouse interface;
- canonical package-name validation as defense in depth;
- a short response cache for expensive GETs;
- no caching of failures or dependency-audit POSTs;
- an explicit first-run state with the active warehouse and setup command;
- `/health` full-SHA provenance;
- `/api/system-health` data-state and source/model/run summaries;
- JSON cleanup for timestamps, numpy scalars, NaN, and infinity.

The service degrades read errors to shape-compatible responses and emits structured logs. This
prioritizes a stable public read surface while keeping failures observable.

## 10. GCP and release engineering

### Bootstrap

`scripts/deploy.sh` requires a clean checkout at a full 40-character SHA. A verified deployment
dispatch additionally requires that SHA to equal remote `main`; dirty/overridden production
releases are rejected. It:

1. enables required APIs;
2. creates or hardens a public-access-blocked, versioned GCS Terraform-state bucket; it selects
   existing remote state as authoritative, or backs up and interactively migrates a validated local
   state to `oss-radar/prod`, and refuses an untracked existing production stack;
3. imports existing Secret Manager containers and Artifact Registry when necessary, after which
   Terraform manages the protected containers/IAM (never secret values/versions) and repository
   with immutable tags;
4. saves every targeted/full Terraform plan and refuses any delete/replace action before apply;
5. provisions the dedicated builder and seven-day source bucket;
6. independently builds or reuses the pipeline and dashboard full-SHA images, so a partial build is
   safely resumable;
7. for a verified release or first greenfield stack, publishes validated secret versions outside
   Terraform state (existing-stack `DISPATCH_DEPLOY=0` preserves live versions), then applies only CD/control-plane
   targets for an existing stack (or the required full greenfield stack);
8. writes five GitHub repository variables:
   `GCP_PROJECT_ID`, `GCP_REGION`, `GCP_WORKLOAD_IDENTITY_PROVIDER`,
   `GCP_DEPLOY_SERVICE_ACCOUNT`, and `GCP_BUILD_SERVICE_ACCOUNT`; and
9. reuses or dispatches the exact-SHA `deploy.yml` run, waits for success, then performs the full
   no-delete Terraform reconciliation and requires a zero-drift plan. `DISPATCH_DEPLOY=0` stops
   after control-plane/build bootstrap for an existing stack.

OIDC trust is restricted to the exact repository name, immutable repository id, immutable owner id,
`refs/heads/main`, and the exact
`MiladShd/oss-radar/.github/workflows/deploy.yml@refs/heads/main` `workflow_ref`. No
service-account JSON key is stored in GitHub. Every third-party Action reference is pinned to an
exact commit SHA.

### Continuous delivery

The deploy workflow triggers only for `pipeline/**`, `dashboard/**`, `infra/cloudbuild.yaml`, or
the workflow itself on `main`, plus manual dispatch. Terraform changes require an explicit
authenticated plan/apply.

The workflow:

1. authenticates through Workload Identity Federation;
2. builds or reuses each immutable full-SHA component independently under the dedicated builder;
3. resolves the tags to `@sha256:` digests and deploys by digest;
4. captures the current pipeline image/SHA, any legacy runtime SHA override, and dashboard revision;
5. stages the dashboard candidate with zero production traffic;
6. validates `/health`, full SHA, and a non-error `/api/system-health` contract;
7. points a permanent isolated smoke job at the pipeline digest and executes an eight-package
   DuckDB dry run with a no-role service account, `/tmp` warehouse, no secrets, dry-run GitHub
   behavior, and the optional LLM disabled;
8. only then updates and verifies the production pipeline job's digest and SHA label, removing any
   legacy runtime `GIT_SHA` so image-baked provenance is authoritative;
9. promotes dashboard traffic, rechecks the public URL, and removes the temporary candidate tag.

On a later step failure, it restores the captured pipeline image/SHA, any prior runtime SHA override,
prior dashboard revision/service SHA label, and the prior candidate-tag state.
Each component also receives an import/provenance smoke test during its independent Cloud Build.

### Terraform ownership and protection

Terraform owns infrastructure shape. Verified CD owns Cloud Run release images and SHA labels;
the full `GIT_SHA` is also baked into each image at build time. Terraform lifecycle ignores those
CD-owned image/label fields so stale bootstrap variables cannot roll production backward.

Artifact Registry, BigQuery, both Secret Manager containers, both artifact/build-source buckets,
and all Cloud Run job/service resources use `prevent_destroy`; Cloud Run deletion protection is
enabled, both buckets enforce public-access prevention with `force_destroy = false`, and durable
data stores do not automatically delete contents. In addition, the bootstrap rejects any plan
containing a delete action, including replacements. Routine `terraform destroy` therefore fails
by design.

Terraform state is remote in a versioned, uniform-access GCS bucket with public-access prevention.
The state bucket itself is bootstrapped out of band so Terraform cannot destroy its own backend.

## 11. Interview-safe claims

Good claims:

- “I deployed a daily GCP data/ML product and made candidate, serving, and deployment provenance
  queryable.”
- “I removed a lookahead bug, quantified shared-package inflation, and added a package-disjoint
  generalization gate.”
- “I compare candidate and incumbent on the same current cohort; non-comparable candidates are
  held.”
- “The growth model has historical cross-sectional ranking evidence. Independent 70-day temporal
  evidence still requires more calendar history.”
- “The risk classifier is Platt-calibrated on package-disjoint OOF predictions and evaluated on an
  untouched reserved-package cohort; categorical safety floors still survive the 60/40 blend.”
- “Watchlist OSV severity is recent but version-unaware; pinned dependency audits are version-aware.”
- “The release path uses remote versioned state, exact Action pins, workflow-bound OIDC, immutable
  tags/digests, an isolated no-role smoke job, and rollback.”
- “The LLM writes optional prose, not predictions.”

Avoid:

- “The model improves every day.”
- “The date-grouped split proves independent future forecasting.”
- “Every source is optional.”
- “Risk reasons explain the classifier.”
- “MLflow is the durable cloud registry.”
- “Every push applies Terraform.”
- “The system is fully autonomous.”

## 12. Highest-value next steps

1. Accumulate enough `download_history` for multiple non-overlapping 70-day temporal cohorts.
2. Validate and tune the 60/40 blend weight against larger closed realized-outcome cohorts.
3. Add a failed-run finalizer and Cloud Monitoring alert for pipeline job failures.
4. Exercise a documented remote-state recovery and release rollback drill.
5. Add uncertainty-aware promotion margins beyond the current risk `0.55` bootstrap floor.
6. Add version selection to watchlist scoring where an installed/pinned version is known.
7. Accumulate deployment history and document the first tested rollback/recovery drill with measured timings.
