You are an independent, adversarial reviewer. Do NOT trust the docs, the PR description, or the blog: re-derive and check. Make NO edits and push nothing; report findings only.

## Context
Repo: ~/oss-radar (GitHub MiladShd/oss-radar), branch `feat/survival-risk` (open PR #185, based on main which already contains PR #184). It is a personal portfolio project (daily GCP data/ML pipeline on public data for 91 Python/AI packages). A blog post for a business audience is about to be published on a personal site and used on a resume, so every number and claim must be defensible.

Files to review:
- `docs/blog/when-the-guarantee-fails.md` (the post; claims to verify)
- `docs/CONFORMAL.md`, `docs/SURVIVAL.md`, `docs/METHODOLOGY.md`, `docs/PORTFOLIO.md` (resume bullets + interview answers)
- `pipeline/oss_radar/models/conformal.py`, `models/growth.py` (`_calibrate_intervals`, `adopt_calibration`, `predict_interval`), `models/scoring.py`, `models/survival.py`, `orchestrator/pipeline.py` (survival hook, borrowed calibration)
- `pipeline/tests/test_conformal.py`, `pipeline/tests/test_survival.py`, `dashboard/tests/test_e2e_browser.py`
- `docs/survival_results.json`, `docs/blog/charts/data.json`, `docs/blog/charts/{build_data,make_charts}.py`, `docs/blog/images/*.svg`
- `infra/cloudflare-proxy/src/index.js`

Setup: `cd ~/oss-radar && .venv/bin/python -m pytest pipeline/tests dashboard/tests -q` (expect 211 passing; browser tests need Playwright Chromium and network for a CDN, and skip otherwise). Lint: `.venv/bin/ruff check pipeline/oss_radar pipeline/tests dashboard scripts`. You will not have BigQuery credentials; work from the committed JSON/CSV-derived artefacts and the code.

## What to verify

### A. Every number in the post, against its source
Build a table: claim | where it appears | source file/field | matches? Cover at least: 58.5% / 79.6% single-split coverage; 67.0/84.3, 71.4/86.2, 75.8/87.4 forward-chained coverage; widths 0.336/0.368/0.410 and "22% wider"; the error-by-date table (0.096/-0.036, 0.157/-0.120, 0.221/-0.191) and "more than doubled"; "7 dates scored"; survival AUC/Brier/mean-predicted ranges (0.82-0.84, 0.054-0.061, 6.6-7.3%, 7.7-8.2%, classifier 0.79-0.82, 0.075-0.167, 17-29%, base rate 0.076-0.087); "2.3x" advisory-history hazard ratio and its CI; "9 of 91 at >=50%, 20 at >=25%, median ~10%"; "211 tests, 23 browser"; "167 events (168 today)"; "21-26 packages on three days". Flag any rounding that flatters (e.g. 58.5 -> 58) or any figure from a different data/feature snapshot than the one it is next to. Check the SVG charts plot exactly the JSON values.

### B. Statistical correctness (read the code, then try to break it)
1. `conformal.py`: weighted conformal quantile (point mass at +inf with weight max(w)); recency weights; `forward_chain` indexing (is each date really predicted using only strictly earlier dates? any off-by-one with `warmup_dates`, `date_index`?); the batch-ACI update `alpha_{t+1}=alpha_t+gamma(alpha-err_t)` and its clipping; `DriftAwareConformal.half_width` (does the final state use the right newest date?).
2. `growth.py::_calibrate_intervals`: residuals come from the train-only tuning model on validation+test; is it valid to apply them to the train+validation deployed model? Is any test-partition information used in a way that contaminates the reported point-forecast test metrics? Is `adopt_calibration` (serving champion borrows the challenger's calibration) defensible, and is it adequately disclosed?
3. `survival.py`: counting-process construction (covariates at t-1, event = vuln_count increase on t; day gaps), event definition on a cumulative count (e.g. one day with a +181 jump counts as one event day - is that handled sensibly?), Breslow partial likelihood, gradient/Hessian, ridge, robust sandwich SE clustered by package, `fit_cox_selected` (leakage?), `event_probability` formula and the "recent baseline hazard" assumption, `landmark_evaluation` (window = days L+1..L+H; any leakage; overlapping landmarks inflate effective n?).
4. Is "calibration" the right word where the post says calibrated probabilities, given the tiny test sets (14-45 positives)? Is the "classifier is worse than guessing the base rate" statement true in exactly the windows stated?

### C. Claims and framing (business audience, resume)
List every sentence that over-states, implies causation without evidence, or could embarrass the author in an interview (e.g. causes of drift, "bulk publication", "scrutiny", "business value"). Check the post never implies customers, production use at a company, or live-traffic results that do not exist. Check "uncertainty" language: does it say clearly what is NOT guaranteed? Suggest exact replacement wording for each issue.

### D. Engineering / security / tests
- Do the tests actually assert what their names claim (look for tautologies, weak thresholds, tests that cannot fail)?
- `infra/cloudflare-proxy/src/index.js`: SSRF/header-forwarding/cache-poisoning risks, caching of query strings, method allow-list, cache key behaviour for `POST /api/audit`.
- `orchestrator/pipeline.py`: failure isolation of the survival step; any case where borrowed calibration or survival scoring can change `risk_score`, promotion, or serving.
- Schema: new nullable columns on `predictions` for DuckDB and BigQuery backends.

### E. Anything else a sceptical senior data scientist or hiring manager would catch.

## Output format
1. A severity-ranked list of findings (BLOCKER / MAJOR / MINOR / NIT): file:line, what is wrong, the evidence or a reproducing command, and a concrete fix.
2. The claim-verification table from A.
3. A short list of claims you could NOT verify and why.
4. A final verdict: is the post accurate enough to publish under the author's name on a resume? If not, the minimal list of changes needed.

Known and already disclosed (do not report as new): only 7 dates scored; evaluation is lag-free while production outcomes take 70 days; settings (gamma, half-life) compared on the same dates; overlapping outcome windows; advisory history alone ranks as well as the survival model; test windows hold 14-45 positives; `statsmodels.PHReg` with entry times did not converge (so it was not used as the reference); the validation gate currently holds the retrained growth model.
