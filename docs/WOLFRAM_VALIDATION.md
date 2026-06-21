# Wolfram validation cross-check — the R² claim, recomputed step by step

This is the **educational, independently-recomputed** companion to [`VALIDATION.md`](VALIDATION.md).
Where `VALIDATION.md` *reports* the growth-model statistics, this tool **re-derives every one of them
from scratch in the Wolfram Language** — printing each calculation Wolfram|Alpha-style (formula →
substitution → intermediates → result) — and **cross-checks** it against the Python harness
(`docs/validation_results.json`). If a deterministic statistic disagrees by more than `1e-6`, the run
exits nonzero so the daily job alarms.

It exists for one reason: *no metric is trusted until an independent engine, on independent code,
reproduces it.* The numbers were already double-checked in NumPy; this proves them a third way.

```
pipeline/
  scripts/validate_growth.py      # Python harness — trains LightGBM, dumps predictions + results JSON
  wolfram/
    validate_growth.wl            # ← the Wolfram cross-check (this tool)
    run_local.sh                  # daily local driver (picks freshest data, publishes the report)
    sample_data/                  # committed held-out predictions so the tool runs out-of-the-box
infra/launchd/…plist              # macOS daily schedule for run_local.sh
infra/terraform/main.tf           # cloud backstop: `oss-radar-validate-daily` scheduler
```

---

## What it verifies (and how each is computed)

Every statistic is recomputed from the **dumped held-out predictions** (`validation_testset.csv`:
`name, t, y, persistence, yhat`), not from the harness's saved numbers.

| # | statistic | formula recomputed | reproduces |
|--:|---|---|---|
| 1 | **R²** (same-package) | `1 − SS_res/SS_tot`, `SS_res=Σ(yᵢ−ŷᵢ)²`, `SS_tot=Σ(yᵢ−ȳ)²` | **0.5821939** |
| 2 | **Spearman ρ** | Pearson correlation of `rank(y)` and `rank(ŷ)` | **0.7903737** |
| 3 | **MAE** | `(1/n)Σ|yᵢ−ŷᵢ|` | 0.1180932 |
| 4 | **Skill vs persistence** | `1 − MSE_model/MSE_persistence` | 0.9410082 |
| 5 | **Calibrated persistence** (fair) | OLS `y~a+b·p` fit on **train**, frozen onto test | b=0.1414, R²=0.0219 |
| 6 | **Diebold–Mariano** | `d̄/SE`, Newey–West HAC var, Bartlett kernel, lag `L=min(h−1,n−1)=69` | −21.251 |
| 7 | **Between-package R²** | collapse each package to its mean, then R² | 0.5905526 |
| 7b | **Head slope** | least-squares slope of `ŷ~c+m·y` (m<1 ⇒ regression to mean) | 0.5520864 |
| 8 | **Package-disjoint R²/ρ** (unseen) | same R²/ρ on GroupKFold out-of-fold preds | **0.363 / 0.683** |
| 9 | **Permutation test** | `p=(1+#{ρ_perm≥ρ_obs})/(1+B)` — *method demo* | p<0.001 |
| 10 | **Cluster bootstrap** | resample whole packages → percentile CI — *method demo* | CI excludes 0 |

Statements 1–7b and 8 are **deterministic** and asserted to `<1e-6`. Statements 9–10 resample, and
Wolfram's RNG differs from NumPy's, so they are reported as **method demonstrations + consistency
bands** (same conclusion: p<0.001, CI excludes 0), never as exact-match assertions.

> The retired **0.74 / 0.70** headline was a centered-MA *lookahead leak*. The honest numbers are
> **0.582 same-package** and **0.363 package-disjoint** — see [`VALIDATION.md`](VALIDATION.md) §1–2.

### Why two extra dumps?

The harness now also writes `validation_trainset.csv` and `validation_oof.csv` (additive,
[`validate_growth.py`](../pipeline/scripts/validate_growth.py)):

- **trainset** — the fair calibrated-persistence baseline is fit *out-of-sample* (slope/intercept
  estimated on the 637 training rows, then frozen onto test). Re-deriving it honestly needs the train
  rows; without them the tool falls back to the harness coefficients and still verifies the test R².
- **oof** — the package-disjoint **0.363** comes from GroupKFold out-of-fold predictions. Dumping them
  lets Wolfram recompute the honest unseen-package number without retraining LightGBM.

The tool **degrades gracefully**: with only `validation_testset.csv` present it still verifies 1–7b
and explains 8 from the harness number.

---

## One-time setup: the free Wolfram Engine

The Wolfram Engine is **free for developers**; activation needs a (free) Wolfram ID.

```bash
# 1. Create a free Wolfram ID:  https://account.wolfram.com/login/create
# 2. Download "Wolfram Engine for Developers" (macOS):  https://www.wolfram.com/engine/
# 3. Open the .dmg, drag "Wolfram Engine.app" to /Applications, launch it once (sets up wolframscript).
# 4. Activate (sign in with your Wolfram ID):
wolframscript -activate
# 5. Verify:
wolframscript -code '1 + 1'        # -> 2
```

Then run the cross-check on the committed sample data:

```bash
wolframscript -file pipeline/wolfram/validate_growth.wl
# or the full driver (publishes docs/validation_steps.md + freshness marker):
bash pipeline/wolfram/run_local.sh
```

Output is the step-by-step terminal report **plus** three files in the data dir:
`validation_steps.md` (rendered-LaTeX educational report), `wolfram_crosscheck.json`
(machine-readable PASS/FAIL per statistic), `wolfram_freshness.json` (last-run marker).

---

## Daily run + cloud backstop

The requirement: run daily, and have a cloud fallback if the local run fails to update.

### Local (the Wolfram educational layer) — macOS `launchd`

```bash
cp infra/launchd/com.ossradar.wolfram-validate.plist ~/Library/LaunchAgents/
launchctl load  ~/Library/LaunchAgents/com.ossradar.wolfram-validate.plist
launchctl start com.ossradar.wolfram-validate          # run once now
launchctl list | grep ossradar                          # check it's registered
```

`run_local.sh` picks the **freshest** data source automatically: `DATA_DIR` env → GCS sync (if
`OSS_RADAR_GCS_BUCKET` set + `gsutil` present) → `/tmp` harness dumps → committed `sample_data`. It
publishes `docs/validation_steps.md` and pushes `wolfram_freshness.json` to GCS. `RunAtLoad` catches a
missed day if the Mac was asleep. (Linux: a `cron` line calling `run_local.sh` works identically.)

### Cloud (the always-fresh numeric backstop) — Cloud Scheduler + Cloud Run

Wolfram Engine in a headless container needs a paid on-demand **entitlement**
(`WOLFRAMSCRIPT_ENTITLEMENTID`), so the cloud fallback deliberately runs the **identical Python math**
(no Wolfram) — same numbers, minus the step-by-step formatting:

- Terraform provisions `oss-radar-validate-daily` (see [`main.tf`](../infra/terraform/main.tf)), which
  triggers the existing Cloud Run job with an args override → `python -m oss_radar.cli validate
  --upload`. It regenerates `validation_results.json` + the three dumps from BigQuery and uploads them
  to `gs://<bucket>/validation/`. Schedule: `var.validate_schedule` (default `30 10 * * *` UTC, after
  the 09:30 pipeline run).
- The `validate` command also runs a **staleness guard**: it reads `wolfram_freshness.json` from GCS and
  emits a structured-log warning (`validate.wolfram_stale`) if the local Wolfram cross-check is older
  than `--staleness-hours` (default 36 h). Wire that log metric to an alert if you want a page.

So even if the local Mac is off for a week, the **authoritative numbers stay fresh in the cloud**; the
Wolfram step-by-step report is the local educational layer that resumes the next time the Mac runs.

```bash
# run the cloud-equivalent locally (needs a populated warehouse):
python -m oss_radar.cli validate --out /tmp/validation            # regenerate only
python -m oss_radar.cli validate --upload                          # + push to GCS
# trigger the cloud job by hand:
gcloud run jobs execute oss-radar-pipeline --region us-central1 \
  --args=validate,--upload --wait
```

---

## Reading a PASS

```
------------------------------------------------------------------------
 R^2  coefficient of determination  (held-out, same-package split)
------------------------------------------------------------------------
   formula     R^2 = 1 - SS_res / SS_tot
   input       n = 182 ; ybar = 0.238057
   step        SS_res = 7.854862 ; SS_tot = 18.800259
   result      R^2 = 1 - 7.854862 / 18.800259 = 0.58219394
   cross-check harness = 0.58219394   |delta| = 1.11e-16   PASS
```

`VERDICT` at the end reports `N/N deterministic cross-checks reproduced to <1e-6`. A nonzero exit means
at least one statistic drifted from the harness — investigate before trusting the dashboard number.
