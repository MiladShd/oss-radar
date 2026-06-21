# OSS Radar — Architecture Study Guide

A guide to learn cold and defend at director level. Every number here is taken from the
code, not memory. Where a number is honest-but-unflattering, that is deliberate — own it.

---

## 1. The 60-second pitch

OSS Radar is a daily AI/OSS-intelligence pipeline. It ingests public signals for a watchlist
of open-source packages (downloads, repo health, ecosystem position, security), engineers
features, trains two LightGBM models (a download-**momentum regressor** and a **risk
classifier**), scores every package into transparent 0–100 numbers with SHAP-driven reasons,
and publishes a daily brief plus a read-only dashboard. A Claude-powered "crew" of role agents
**runs** the pipeline — monitoring, filing GitHub incidents, writing the brief, and proposing
model improvements via PR — but the agents **never produce the predictions**, so forecasts stay
deterministic and backtestable.

What makes it director-grade isn't the scope (one person, ~91 packages). It's the **engineering
and statistical discipline**: one Docker image runs hermetically on DuckDB locally/CI and on
serverless BigQuery in prod (one env var); secrets never touch repo/Terraform-state/logs;
least-privilege IAM; never-500 graceful degradation everywhere; safe self-improvement gated on a
measured held-out lift behind a human-merged PR; and an **adversarial validation layer that
dismantles my own headline metric** — I found and published two leaks that took the growth R²
from 0.74 to an honest 0.36 rather than ship the flattering number.

The one-line framing for each claim: the **growth model is a cross-sectional ranked momentum
watchlist, not a time-forward forecaster** — and I prove that rather than hide it. The genuine
short-horizon forecasting claim lives in a separate daily-series analysis (ETS, MASE 0.69 at
1 day, decaying to persistence by day 7).

---

## 2. End-to-end data flow — one daily run, stage by stage

Trigger: Cloud Scheduler fires `'30 9 * * *'` (09:30 UTC) and POSTs the Run Admin API
`jobs:run` endpoint with an OAuth token from a dedicated SA scoped to `run.invoker` on exactly
that one Cloud Run Job. The job runs `run_pipeline(settings, dry_run)` — the entire DAG as one
linear in-process Python function. It mints **one UTC `run_id` (`%Y%m%dT%H%M%SZ`)** at the top
and threads it through every write; each stage is timed into a `stages` dict.

1. **Ingest + heal.** `collect()` fans out one snapshot row per package across a
   `ThreadPoolExecutor(max_workers=4)`. Each worker calls 6 free no-auth APIs (ecosyste.ms,
   PyPI JSON, pypistats, OSV, deps.dev, GitHub REST), merges fields per a documented
   `_first()` precedence ladder, and records a per-source `_ok` map into `source_status`.
   `heal()` then finds packages with `downloads_7d IS NULL`, retries them once single-threaded,
   and for any still-missing **carries forward** the last-good warehouse snapshot re-stamped with
   the current `run_id`/date. The warehouse `truncate`s `download_history` and reloads it; inserts
   the snapshots.
2. **Features.** Build the growth training frame (slide an as-of cursor, stride 3, min_history 84,
   over each package's 28-day **trailing/causal**-smoothed series) and the growth scoring frame
   (one row/package, latest as-of). Build the risk frame from latest snapshots.
   `choose_risk_training` returns `risk_label_mode = "forward-outcome"` once snapshot history
   spans `risk_horizon_days=14` with `>= forward_min_rows=25` rows and both classes present,
   else `"heuristic"`.
3. **Train.** `GrowthModel.fit` sorts by `feature_date`, clips the target to `[-0.9, 3.0]`, takes
   the first 80% as train / last 20% as test (**time-aware, not shuffled**), fits an LGBMRegressor
   with early stopping. `RiskModel.fit` imputes feature medians and trains an LGBMClassifier only
   when class balance allows (`3 <= n_pos <= n-3`), estimating AUC via StratifiedKFold
   cross-val-predict; otherwise returns `model=None`. Both use `random_seed=42`.
4. **Register.** `ModelRegistry.persist` looks up the prior champion's best primary metric
   (Spearman for growth, AUC for risk) and promotes only on strict improvement beyond
   `PROMOTION_MARGIN=0.0`. Every run is persisted regardless; artifacts go to GCS, params/metrics
   to MLflow.
5. **Score / drift / backtest.** `build_predictions` converts growth pred → momentum
   (`100*sigmoid(3*pred)`), computes the risk composite, blends in the classifier probability
   `0.6*composite + 0.4*proba*100` when the model trained, and attaches SHAP top-k reasons.
   `compute_prediction_drift` runs **before** today's predictions are written, so its
   `ORDER BY predicted_at DESC LIMIT 1` query returns the **prior** run as the PSI baseline. A
   held-out backtest payload (10-decile calibration + scatter for growth; ROC + AUC for risk) is
   persisted.
6. **Agents.** `run_crew` runs 8 role steps (Healer, DataEngineer, DataQuality, DataScientist,
   ModelMonitor, ImprovementScientist, RiskAnalyst, MLOps). Each calls `ctx.record(...)` appending
   a uniform activity row. Side effects (GitHub issues/PRs, the daily brief PR) are gated on
   `dry_run` **and** `github_token`. The ImprovementScientist may open a PR editing
   `active_features.json` if an offline experiment clears the lift bar.
7. **Persist.** Write predictions, model_runs, agent_activity, features, and a `pipeline_runs`
   audit row (stages, counts, `git_sha`).

Idempotency comes from `run_id` keying at the **read** layer (consumers select the latest run),
not from DB constraints — which keeps the schema portable across DuckDB/BigQuery.

---

## 3. Subsystems — what / why / how + the decision + how to defend

### 3.1 Ingestion
**What:** 6 free, no-auth public HTTP APIs fanned out one snapshot row per package over a
polite per-host-throttled client with tenacity retries, then self-healing of transient holes.
**Why these sources:** zero marginal cost, zero credential management; each is best-in-class for
different fields (ecosyste.ms = freshest stars/forks + reverse-deps + bus factor; GitHub = sole
source of commit volume + PR/issue velocity; deps.dev = sole OpenSSF Scorecard; OSV = sole vuln;
PyPI = canonical release cadence). No single source is load-bearing because `_first()` falls back.
**How:** `HttpClient._request` applies per-host throttling via a per-host `threading.Lock` + a
`monotonic` last-call timestamp; treats **404 → None** (normal "unknown package"), raises
`RateLimited` on **429/403/5xx** so `@retry` (`wait_exponential 1..20s, stop_after_attempt(4),
reraise`) backs off, and `get_json` swallows everything else to `None` so connectors degrade.
`heal()` is two-phase: retry NULL-downloads once single-threaded, then `_carry_forward` the
last-good warehouse row.

**Key decisions & defense:**
- *Per-host min-interval throttle + per-host lock, not a global token bucket.* Each API is
  independent fair-use with its own sensitivity (pypistats 1.0s, api.github.com 1.2s,
  ecosyste.ms 0.2s, default 0.1s). The per-host lock guarantees 4 concurrent workers can't burst
  one host. **Defend:** the read-modify-write of `_last_call[host]` happens inside
  `with self._locks[host]`, so the floor holds under concurrency; speedup comes from
  parallelizing *across* hosts.
- *404 graceful, 403 folded into the retry path.* GitHub returns 403 for secondary rate-limit,
  not just auth, so 403 is retryable. **Defend:** explicit branches in `http.py`; 422 (malformed
  GitHub search) falls through to the broad except → `None`, which only zeroes optional velocity
  fields — graceful, not a crash.
- *Carry-forward over NULL or interpolation.* A stale-but-real row beats a hole that craters
  downstream features. **Defend:** failure is narrowly `downloads_7d IS NULL`; `_carry_forward`
  only copies a row that itself had non-null downloads; `stats.carried_forward` + per-source
  `source_status` make staleness **auditable, not silent**.

**Hardest question:** *"`_carry_forward` interpolates the package name into SQL — injection?"*
Mitigated by a `_SAFE` allowlist (strip non-`[A-Za-z0-9_.-]`, cap 80 chars) and the input is the
curated watchlist, not user input. Honest answer: parameter binding is the correct fix; I'd flag
it as a hardening item; blast radius today is nil.

### 3.2 Warehouse
**What:** a `Warehouse` ABC with two interchangeable backends — `DuckDBWarehouse` (local file)
and `BigQueryWarehouse` (cloud) — sharing one Python-defined **8-table** schema, one portable-SQL
dialect, and one row-coercion path, selected by a single `OSS_RADAR_BACKEND` env var.
**Why:** the exact same image and code run hermetically in dev/CI/tests (DuckDB, no creds,
instant, free) and in prod (BigQuery, serverless). Tests need no GCP project, billing, or network.
**How:** `schema.TABLES` is the single source of truth driving DDL, row coercion (`prepare_rows`
→ `_coerce`), and INSERT column ordering for both backends — drift is structurally impossible.
3 moves keep one SQL string working on both engines: **JSON stored as serialized STRING** in both
(VARCHAR/STRING, not native JSON); **all date math in pandas**, not SQL; and `default_dataset` set
on every BigQuery `QueryJobConfig` so bare-table SQL (`SELECT * FROM snapshots`) resolves on both.
BigQuery writes use **WRITE_APPEND load jobs** (free) not streaming inserts (billed, buffer
semantics). `factory.get_warehouse` lazily imports `google.cloud.bigquery` only in the bigquery
branch, so the heavy dep never loads locally.

**Key decisions & defense:**
- *ABC over coding directly against BigQuery.* The interface is 4 abstract methods + 2 helpers —
  abstraction cost near zero, payoff large (hermetic CI). **Defend:** lazy import proves BigQuery
  deps never load in tests; `test_warehouse.py` runs with no GCP.
- *Schema once in Python, per-backend type maps.* One edit adds a column to both backends.
  **Defend:** the same `TABLES` dict literally generates VARCHAR in DuckDB and STRING in BigQuery.
- *Append-only + `run_id` as the version key, no PKs/UNIQUE.* A retried run produces a new
  `run_id` that supersedes the old on read; stale runs are harmless history. Only
  `download_history` is truncated each run (full rebuilt backfill). **Defend:** BigQuery doesn't
  enforce PKs anyway; idempotency lives at the read layer.

**Hardest question:** *"JSON-as-string means you can't query JSON internals in SQL — limitation?"*
Deliberate and scoped: those columns are always read whole and `json.loads`'d in Python; the
queryable dimensions are first-class typed columns. If a JSON field needed to be queryable, promote
it to a typed column (one-line schema edit), not native JSON.

### 3.3 Features
**What:** two feature builders. Growth: a multi-horizon download-dynamics regressor — **14 active
features** (7 short + 7 long horizon), 3 dormant candidates, a **70-day log-growth target** on a
28-day causal-smoothed series. Risk: a cross-sectional classifier (**13 features**) whose label
evolves from a transparent heuristic to realized forward outcomes as history accumulates.
**Why the causal smoother:** a centered MA at date *t* averages *t−14..t+14*, leaking up to 14 days
of **future** into a feature used to forecast the future. The trailing MA only uses days ≤ *t*, so
training rows are identically distributed to the live scoring row.
**How:** `_smooth` is a 28-day trailing MA; `_download_features` computes features at an as-of date
via offset windows and mean-normalized OLS slopes; `build_growth_training` slides a stride-3 cursor
(min_history 84) and sets `growth_target_7d = log1p(future-70d) − log1p(trailing-70d)`. Risk label
starts as `_at_risk_label` (heuristic OR), then `forward.py` relabels on realized escalation; the
target is clipped to `[-0.9, 3.0]` at train time.

**Key decisions & defense:**
- *Causal smoother.* **Defend:** `validate_growth.py` swaps **only** the smoother on an identical
  pipeline/split; R² drops 0.7015 → 0.5822 while Spearman barely moves — the signature of a
  level/calibration leak, not a worse smoother.
- *70-day horizon on a 28-day-smoothed series.* Raw 7-day growth is near-noise (R² ~0); a 10-week
  momentum target is genuinely learnable and the more decision-useful signal. 70 days is also
  capped by the 180-day pypistats retention window (2×horizon + 84 min-history fits in 180).
  **Defend:** target is a log **ratio**, so the download level divides out — skill can't come from
  "big stays big."
- *Candidate features computed every run, only active when listed in `active_features.json`.* Lets
  the self-improvement agent A/B test cheaply while the live model stays frozen until a reviewed PR.
  **Defend:** `active_download_features()` filters to features the code actually computes and falls
  back to defaults if the file is malformed — an allowlist over a code-defined catalog.

**Hardest question:** *"The risk label is hand-written rules — isn't that just an opinion?"* It's a
transparent cold-start prior (documented thresholds: HIGH/CRITICAL vuln in 28d, archived/removed,
>365d stale, key-person = `bus_factor < 0.1 AND dependents > 1000`). It's explicitly temporary —
`choose_risk_training` auto-switches to realized forward outcomes once ≥25 rows with both classes
exist.

### 3.4 Models (growth, risk, scoring, backtest, drift, experiment) + registry
**What:** LightGBM growth regressor + risk classifier, time-aware splits, transparent 0–100 scores
with SHAP reasons, champion/challenger promotion, PSI drift monitoring, GCS artifacts + MLflow.
**Why LightGBM:** small tabular cross-sectional data (~637 windowed rows, 13–14 features) is exactly
where GBDTs dominate and neural nets overfit; deterministic, fast, native mixed-scale handling, gain
importances, exact fast TreeExplainer SHAP for per-row reasons.
**How:** growth = LGBMRegressor (500 trees, lr 0.03, num_leaves 31, early_stopping 40, L1) on an
80/20 time split. Risk composite weights: **vulns 0.24, staleness 0.20, key-person 0.18, security
posture 0.16, abandoned 0.12, backlog 0.10**; blended `0.6*composite + 0.4*proba*100` only when the
model trained. Momentum labels high ≥66 / declining ≤40; risk levels high ≥66 / medium ≥40. PSI
bands: <0.10 low, 0.10–0.25 moderate, >0.25 high; label-churn bands 0.15 / 0.30.

**Key decisions & defense:**
- *Time-aware split, not random/k-fold.* A random split lets the model see future rows for the same
  package. **Defend:** the same split is reproduced in `backtest.py` and `experiment.py`, so the
  dashboard and feature experiments measure the same honest number.
- *Risk = transparent composite, ML blended in only when class balance allows.* The watchlist is
  small and mostly healthy; a classifier alone is unstable and opaque. **Defend:** composite never
  NaNs; `predict_proba` returns NaN and the blend safely falls back when the model didn't train.
- *Champion gate on a single primary rank metric, strict improvement.* Prevents silent regression;
  every run still persisted for auditability. **Defend, including the weakness:** `PROMOTION_MARGIN`
  is currently 0.0, so any epsilon wins — but the fixed seed makes metrics deterministic so a
  promotion reflects a real change, the metric is a rank metric on an out-of-time tail, and the
  margin dict exists exactly so it can be raised (with a bootstrap-CI / min-test-size guard).
- *PSI drift, not KS or perf monitoring.* The 70-day outcome isn't observed at scoring time, so
  ground-truth perf monitoring is impossible run-to-run; PSI is the label-free industry standard.
  **Defend:** quantile bins, `eps=1e-4` floor, paired with categorical label churn.

**Hardest question:** *"The column is named `growth_target_7d` but you predict 70-day growth — which
is it?"* It's the 70-day log-growth target; the name is legacy/misleading documentation debt, not a
logic bug — `GROWTH_HORIZON=70`, `forward.py` computes `log1p(future_70d) − log1p(this_70d)`. I'd
rename it.

### 3.5 Agents
**What:** a Claude-powered crew that **manages** the pipeline (monitoring, reporting,
incident-filing, propose-only self-improvement) — never produces predictions.
**Why:** keeps the LLM out of the prediction path so forecasts stay deterministic, reproducible, and
backtestable; the uniform `agent_activity` log turns opaque automation into a queryable public audit
trail on the dashboard.
**How:** `use_llm` gates real Claude vs deterministic templates on the key prefix
(`anthropic_api_key.startswith('sk-ant')`); a sentinel like `DISABLED` runs the full crew for $0.
`_risk_analyst` **always** builds the deterministic template first and overwrites it only if Claude
is available and returns text, labeling the activity `claude` vs `template`. Self-improvement is
propose-only: a branch-idempotent PR editing `active_features.json`, opened only when
`experiment.evaluate_candidates` measures a held-out Spearman lift `>= feature_lift_margin=0.01`.
GitHub ops degrade gracefully — every function returns `None` on failure and the caller records a
`skipped`/`warning`, so GitHub being down never fails the data run.

**Hardest question:** *"These aren't real autonomous agents — where's the agency?"* Correct and
intentional. They're role-scoped deterministic controllers with one LLM touchpoint (the brief) and
one genuinely autonomous-but-PR-gated decision loop (the ImprovementScientist). I deliberately
avoided free-roaming LLM agents because the pipeline produces intelligence that must be reproducible.
The numbers in the brief come from DataFrames; Claude writes only the ≤300-word narrative.

### 3.6 Orchestrator
**What:** the whole DAG as a single linear function (`run_pipeline`), CLI via argparse, triggered
daily by Cloud Scheduler → Cloud Run Job.
**Why one function, not Airflow/Prefect:** strictly-sequential dataflow where each stage consumes
the prior stage's in-memory DataFrame — no fan-out to parallelize, no metadata DB needed. Cloud
Scheduler gives the cron, Cloud Run gives execution + retry.
**How:** one `run_id` threaded everywhere; per-stage timing into a `stages` dict; a `pipeline_runs`
audit row gives the observability a DAG UI would.

**Hardest question:** *"If a stage crashes mid-run, what's left behind?"* Writes are interleaved, so
a crash can leave a partially-written warehouse (`download_history` is truncate-then-insert early;
no `failed` `pipeline_runs` row is written — only logs). Recovery is by re-run: `run_id` is fresh so
retries don't collide, `max_retries=1` + daily cadence self-heals, and data is regenerable. The
hardening item I'd volunteer: a `try/finally` writing `status='failed'` + the partial stages dict,
plus failure alerting on the job's `failed_count`.

### 3.7 Dashboard
**What:** a read-only FastAPI service serving a single-file vanilla-JS SPA (**582 lines**) + **7**
thin JSON endpoints over the warehouse, with a defensive JSON-sanitization layer and never-500
handling, deployed scale-to-zero on Cloud Run.
**Why single-file SPA:** read-only, ~7 views, no auth or write paths — Chart.js (pinned 4.4.1) from
CDN is instantly deployable, zero build/CI toolchain, zero npm supply-chain surface, trivially
auditable, and pairs perfectly with scale-to-zero (no Node runtime).
**How:** every data endpoint is wrapped in `_safe(fn, default)` that catches all exceptions, logs
`api.query_failed`, and returns a shape-matched empty default. `_clean` (17-line recursive) maps
numpy→py, **NaN/inf→null** (the specific bug that breaks browser `JSON.parse`), timestamps→ISO. The
one user-controlled path (`package_detail`) is sanitized by the same allowlist before interpolation.
Expensive offline audit artifacts (validation/timeseries) are committed as static JSON; cheap per-run
data goes through the live API.

**Hardest question:** *"`_safe` hides all errors — how would you know it's broken?"* Failures aren't
silent at the ops layer — every catch logs `api.query_failed` to Cloud Run logs (alertable). The
user-facing degradation (empty states) is deliberate for a public read-only surface where a 500 is
worse than "no data yet." Availability over fidelity, observable where it matters.

### 3.8 Infra / CI-CD
**What:** a Terraform-defined, scripted-bootstrap GCP stack: daily Scheduler → Cloud Run **Job**
(pipeline), public scale-to-zero Cloud Run **Service** (dashboard), 2 images, BigQuery + GCS, **3
least-privilege service accounts**, two-tier GitHub Actions CI.
**Why a Job for the pipeline:** it's finite batch work — a Job is the native run-to-completion
primitive, exits when done, $0 between runs. A Service is for long-lived request handlers (60-min
cap, paid to keep warm).
**How / defense highlights:**
- **Secrets** exist only in Secret Manager, created out-of-band by `deploy.sh` via stdin; TF
  references secret **IDs** only, never values → no secret material in repo, Terraform state, or
  logs. `version='latest'` enables rotation without redeploy.
- **3 SAs:** pipeline = `dataEditor + jobUser + objectAdmin + 2× secretAccessor`; dashboard =
  `dataViewer + jobUser` (read-only, **zero secret access**); scheduler = `run.invoker` on **one
  job**. The dashboard SA can never mutate data or reach secrets.
- **Two images** from one monorepo, built by Cloud Build for **linux/amd64** (avoids the
  Apple-Silicon arm64-on-amd64 silent Cloud Run failure). Pipeline image installs `libgomp1`
  (LightGBM's OpenMP runtime); dashboard image is slim with `--no-deps` and no ML libs.
- **TF pins the immutable git-SHA image tag** (not `:latest`) for reproducible rollback.
- **Two-tier CI:** `ci.yml` = ruff + pytest; `pr-preview.yml` runs the **actual pipeline**
  creds-free in DuckDB mode (`run --dry-run --limit 8`, only the auto-provided `GITHUB_TOKEN`) and
  posts a movers comment on the PR — proving the same code runs locally and in cloud.

**Hardest question:** *"`terraform.tfstate` is committed — secret leak?"* It contains **no** secret
material by design (TF references IDs, secrets created out-of-band). Honest weakness to concede: a
committed local state file is wrong for a team — for anything beyond solo I'd move to a remote GCS
backend with state locking/versioning. The mitigation that makes it safe today is that it can't leak
a key it never contained.

---

## 4. Cross-cutting decisions

1. **Dual-backend warehouse, one env var.** Same image runs hermetically (DuckDB) and serverlessly
   (BigQuery); drift structurally impossible via one schema source of truth. Trade-off: JSON-as-string
   (read whole in Python) and pandas-side date math — both deliberate to keep one SQL dialect.
2. **LightGBM, not deep learning / deep TS.** Matched to the small-tabular cross-sectional regime;
   deterministic; SHAP reasons. Genuine TS work is done separately with ETS/SARIMA on the daily series.
3. **Daily batch via Scheduler → Cloud Run Job, DAG as one function.** Data updates at most daily;
   no fan-out to parallelize; cadence lives in one Terraform var. Trade-off: no per-stage retries /
   no transaction — accepted because `run_id` + daily cadence self-heal and data is regenerable.
4. **6 free no-auth APIs + per-field `_first()` precedence.** Zero cost, each source best-in-class
   for its fields, no single point of failure. Trade-off: no SLA — mitigated by throttle + tenacity
   + two-phase self-healing with auditable carry-forward.
5. **AI crew manages, never predicts.** LLM out of the prediction path; uniform queryable audit log;
   self-improvement is propose-only behind a measured lift + human merge.
6. **Adversarial statistical-honesty layer.** Validates the production feature code, isolates leaks
   with controlled experiments, reports honest package-clustered inference vs a fair baseline, and
   commits results as version-controlled JSON.

---

## 5. The statistical-honesty story — why R² went 0.74 → 0.36, and why it's a STRENGTH

This is the headline talking point. The arc is "0.74 → 0.36," and I found and published both leaks
myself rather than ship the flattering number.

**The original headline (0.74) was inflated by two separate leaks:**

| Stage | R² | Spearman | Why |
|---|---|---|---|
| Leaky headline (centered MA) | 0.740 / 0.7015 | 0.826 / 0.780 | lookahead leak |
| Same-package, causal smoother | 0.582 | 0.790 | still leaks package identity |
| **Unseen-package (GroupKFold)** | **0.363** | **0.683** | **the honest cross-sectional skill** |

*(0.74 is the historical original pull; 0.7015 is the exact value reproduced on today's 91-package
warehouse — reconcile the text, same story.)*

**Leak 1 — lookahead.** A centered moving average at date *t* averages *t−14..t+14*, peeking 14 days
into the future. `validate_growth.py` runs the **identical** pipeline/split swapping **only**
centered vs trailing smoother; R² drops 0.7015 → 0.5822 while Spearman barely moves (~+0.01). That
isolation is what makes it a *leak*, not a tuning artifact — a level/calibration leak inflates R²,
not rank.

**Leak 2 — shared-package memorization.** All 91 packages were in both train and test. GroupKFold by
package (train/test share zero packages) gives the honest unseen-package **R² 0.363 / Spearman 0.683**.

**Why the honest number is still real skill:** the target is a log **ratio** (download level divides
out, so it's not "big stays big"), it generalizes to **unseen** packages, and it beats a **fair
calibrated-persistence baseline** (regress `y ~ a + b*persistence`; raw persistence is a scale-broken
strawman at R² −6.08) — honest gap is model 0.58 vs fair 0.02 on R², 0.79 vs 0.37 on rank, with
permutation p < 0.001.

**Why inference is package-clustered:** 182 test rows are heavily autocorrelated (70-day windows,
stride 3 → ~23× overlap → effective n ≈ 91, not 182). So: cluster bootstrap resampling whole
packages (95% CI on R² ≈ [0.37, 0.72]), block permutation preserving within-package autocorrelation,
and Diebold-Mariano with Newey-West HAC at lag h−1=69. Temporal CV is declared **infeasible** (9
origins span 24 days < the 70-day horizon → ~0.34 independent forecasts in time; purged walk-forward
= 0 folds) — so I ship it as **cross-sectional and say so**, never dressing overlapping data as
temporal skill.

**The separate, honest forecasting claim:** `timeseries_analysis.py` runs a rolling-origin battery
on the daily log series where hundreds of origins genuinely exist. Weekly-seasonal ETS gets MASE
**0.69 at h=1** vs seasonal-naive 0.98 (beats it in 100% of packages, DM p ~1e-6), stays ahead at
h=3, and reaches **parity by h=7** — so I cap the forecast claim at ~3 days. It also fixed a real bug
(ARCH/Jarque-Bera run on the STL remainder, not `np.diff`, dropping fake 93% heteroscedasticity to an
honest 70%) and honestly withholds the calibration claim (PICP@95 = 0.921 undercovers, MZ slope 0.84).

**How to talk about it:** "I refuse to call the growth model a forecaster — it's a ranked momentum
watchlist for one origin, and I prove it. Finding and publishing my own leaks is the whole point: a
defensible 0.36 with an audit trail is worth infinitely more than an indefensible 0.74."

---

## 6. Defend your decisions — the hardest Q&A

- **"R² was 0.74 — defend it or admit it's wrong."** It's wrong and the repo says so. Two leaks,
  isolated by controlled experiments, honest unseen-package R² 0.363 / Spearman 0.683 beating a fair
  baseline with p < 0.001. The defensible claim is rank skill on a cross-sectional watchlist.
- **"How does this scale to 1M packages?"** Ingestion is rate-limit-bound, not compute-bound — shard
  across hosts/tokens, persist incrementally. Warehouse: flip the env var to BigQuery (the abstraction
  exists for exactly this); only concern is `query_df` materializing — add a streaming variant or push
  aggregation into SQL. Modeling: LightGBM trains on millions of rows; more packages strengthens the
  cross-sectional validation. The one-function DAG is the only piece needing lifting into an engine
  if fan-out appears — stages are already clean module boundaries.
- **"What does it cost, and where does it blow up first?"** A few dollars/month: scale-to-zero
  dashboard ($0 idle), Cloud Run Job pays only for minutes/day, BigQuery free WRITE_APPEND load jobs,
  LLM off by default. First blow-up is BigQuery bytes-scanned if traffic grew — mitigated by LIMIT
  8/30/80 queries and a `run_id`-keyed TTL cache (trivially correct since data changes once per run).
- **"Two warehouse backends — NIH?"** A ~4-method ABC, not a database. Buy-alternatives all lose:
  BigQuery-only kills the inner loop; SQLAlchemy adds a dialect-translation layer between two backends
  that already share ANSI SQL. Targeted portability, not a framework.
- **"You interpolate names into SQL — injection."** Candid: parameter binding is the textbook fix and
  I'd switch on request. Write path is already parameterized; read path uses an allowlist sanitizer
  (`[A-Za-z0-9_.-]`, cap 80); the only public path (dashboard `package_detail`) is exactly why it has
  the allowlist. I chose allowlist partly because DuckDB/BigQuery placeholder syntaxes differ. Residual
  risk for this charset is nil; threading params through `query_df` is my top hardening follow-up.
- **"Promotion margin is 0.0 — promotes on noise?"** Known weakness; `PROMOTION_MARGIN` exists to be
  raised. Protected by the fixed seed (deterministic metrics → promotion reflects a real change), rank
  metrics on an out-of-time tail, and full persistence for reversibility. Fix: min margin + bootstrap-CI
  guard (the harness already computes the CI).
- **"Walk me through every failure mode."** Source-down-for-one-package → one field drops, never the
  run; throttling → tenacity backoff; source-down-for-everyone → carry-forward, observable via stats +
  `source_status`; stage crash → next run self-heals (gap: no `failed` row, hardening item); GitHub down
  → `skipped`/`warning`, report still writes; dashboard query fails → `_safe` empty default, never 500s.
- **"If you can't temporally validate growth, why believe it forecasts?"** I don't claim it does —
  cross-sectional, and I prove it (purged walk-forward = 0 folds). The temporal claim lives in the
  daily-series harness, capped at ~3 days. Two models, two honestly-scoped claims.
- **"One person's resume project — why trust the judgment?"** Because the hard parts are the
  production concerns: reproducibility (one seed, `run_id`, SHA-pinned images), least privilege (3 SAs,
  read-only dashboard SA, secrets out of state), graceful degradation everywhere, PR-gated automation,
  and intellectual honesty (published my own leaks). Small scope, senior-level discipline.

---

## 7. Numbers to memorize

**Ingestion** — 6 free no-auth sources; per-host floors pypistats 1.0s / api.github.com 1.2s /
ecosyste.ms 0.2s / default 0.1s; tenacity `wait_exponential 1..20s, stop_after_attempt(4)`;
`ThreadPoolExecutor max_workers=4`, healing single-threaded; `http_timeout=30s`; 404→None,
429/403/5xx→RateLimited; failure = `downloads_7d IS NULL`; name sanitized + truncated to 80 chars.

**Warehouse** — 8 tables (snapshots, download_history, features, predictions, model_runs,
agent_activity, pipeline_runs, backtest); 7 abstract types; 4 abstract methods + 2 helpers; default
backend DuckDB, switch = `OSS_RADAR_BACKEND`; snapshots is the widest (~40 cols); JSON → VARCHAR
(DuckDB) / STRING (BigQuery); WRITE_APPEND load jobs; only download_history truncated each run.

**Features / models** — 14 active download features (7 short + 7 long) + 3 candidates; 13 risk
features; `GROWTH_HORIZON=70`, `SMOOTH_WINDOW=28` (causal), stride 3, min_history 84; target =
`log1p(future_70d) − log1p(this_70d)` clipped `[-0.9, 3.0]`; 80/20 time-aware split; GrowthModel 500
trees / lr 0.03 / num_leaves 31 / early_stopping 40; RiskModel 200 trees, trains if `3 ≤ n_pos ≤ n−3`;
momentum = `100*sigmoid(3*pred)` (high ≥66 / declining ≤40); risk blend `0.6*composite + 0.4*proba*100`
(high ≥66 / medium ≥40); composite weights 0.24/0.20/0.18/0.16/0.12/0.10; `PROMOTION_MARGIN=0.0`;
PSI bands <0.10 / 0.10–0.25 / >0.25, churn 0.15 / 0.30.

**Validation** — leaky 0.7015 → causal 0.582 → unseen-package **0.363 R² / 0.683 Spearman**;
fair baseline R² 0.02 / Spearman 0.37; cluster-bootstrap 95% CI on R² [0.37, 0.72]; permutation
p < 0.001 (1/2001); n=182 but effective ≈ 91 (~23× overlap); 91 packages; 9 origins / 24-day span /
~0.34 independent temporal forecasts. TS: ETS MASE 0.69 (h=1) → parity (h=7); 70% heteroscedastic
(STL remainder); PICP@95 0.921, MZ slope 0.84.

**Config / orchestrator** — `random_seed=42`, `min_train_rows=200`, `feature_lift_margin=0.01`,
`risk_horizon_days=14`, `forward_min_rows=25`, `growth_horizon_days=14`; `run_id` = `%Y%m%dT%H%M%SZ`;
9 DAG stages; `collect()` max_workers 4.

**Agents / LLM** — `llm_model=claude-opus-4-8`, `llm_max_tokens=1600`; `use_llm` gate = key starts
`sk-ant` (sentinel `DISABLED` → template mode); brief cap ≤300 words; 8 crew steps; `agent_activity`
LIMIT 80; PR branches `oss-radar/daily-{date}`, `oss-radar/feature-{candidate}`.

**Infra / CI** — pipeline Job 2 vCPU / 4Gi, timeout 1800s, max_retries 1; dashboard Service 1 vCPU /
1Gi, min 0 / max 2, port 8080; schedule `'30 9 * * *'` UTC; region us-central1, dataset oss_radar;
Cloud Build E2_HIGHCPU_8 / linux-amd64; CI ruff 0.8.4 + pytest 8.3.4 / Python 3.12; PR-preview DuckDB
`--limit 8`, only `GITHUB_TOKEN`; 3 SAs; 2 secrets injected via `secret_key_ref version='latest'`;
TF pins git-SHA image tag; dashboard SPA 582 lines, Chart.js 4.4.1, 7 API endpoints.

---

## 8. What I'd do next with more time/budget

1. **Parameterize all read-path SQL** — thread query parameters through `query_df` (DuckDB `?` /
   BigQuery `ScalarQueryParameter`) so the allowlist becomes defense-in-depth, not the primary control.
2. **Harden the orchestrator** — `try/finally` writing a `pipeline_runs status='failed'` row with the
   partial stages dict, plus a Cloud Monitoring alert on the Job's `failed_count`.
3. **Tighten promotion** — raise `PROMOTION_MARGIN` above 0.0 and gate on a bootstrap-CI / min-test-size
   check (the harness already computes the CI) so a noisy epsilon can't flip the champion.
4. **Remote Terraform state** — move `tfstate` to a GCS backend with locking + versioning (the local
   committed file is solo-only).
5. **Fix the documentation debt** — rename `growth_target_7d` → `growth_target_70d`, and update the
   stale `engineering.py` docstring that still cites 0.74 to point at VALIDATION.md's honest numbers.
6. **Grow the validation surface** — more packages → more independent cross-sectional units, and once
   enough forward-outcome risk labels accumulate, tune the 0.6/0.4 risk blend weights against realized
   outcomes instead of hand-setting them.
7. **Dashboard caching** — a `run_id`-keyed TTL cache (trivially correct since data changes once per
   run) and possibly a materialized overview table if traffic ever justified it.
8. **Pin images by digest** rather than git-SHA tag for fully immutable deploys.
