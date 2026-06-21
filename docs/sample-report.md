# OSS Radar — sample output

> A real, unedited run of the full pipeline (ingest → features → train → score → agents) on **40 packages** against free public data sources, written to a local DuckDB warehouse. Generated from run `20260621T012704Z` (2026-06-20). Refresh with `make demo && python scripts/demo_report.py`.

_Tracked 40 packages · growth model Spearman 0.097 · risk model AUC 0.576 · download coverage 100%_

## 🚀 Top momentum movers

| Package | Momentum | Pred 7d growth | Why |
|---|--:|--:|---|
| `metagpt` | 60 | +14.2% | this week running above the monthly average, strong weekly download base, release staleness, maintainer key-person risk |
| `e2b` | 60 | +13.9% | weekly downloads accelerating vs prior week, strong monthly download base, maintainer key-person risk, weak security posture |
| `langflow` | 59 | +12.2% | weekly downloads accelerating vs prior week, small monthly download base, recent vulnerabilities, maintainer key-person risk |
| `weaviate-client` | 59 | +12.2% | weekly downloads accelerating vs prior week, 28-day download trend falling, issue backlog pressure, maintainer key-person risk |
| `trl` | 59 | +12.1% | strong monthly download base, volatile download pattern, maintainer key-person risk, weak security posture |
| `haystack-ai` | 59 | +11.9% | weekly downloads accelerating vs prior week, small monthly download base |
| `pyautogen` | 59 | +11.7% | weekly downloads accelerating vs prior week, small monthly download base, release staleness, issue backlog pressure |
| `dspy-ai` | 58 | +10.9% | weekly downloads accelerating vs prior week, small monthly download base, issue backlog pressure, maintainer key-person risk |

## ⚠️ Top rising dependency risk

| Package | Risk | Level | Why |
|---|--:|---|---|
| `langflow` | 63 | medium | weekly downloads accelerating vs prior week, small monthly download base, recent vulnerabilities, maintainer key-person risk |
| `metagpt` | 62 | medium | this week running above the monthly average, strong weekly download base, release staleness, maintainer key-person risk |
| `pinecone-client` | 62 | medium | small monthly download base, small weekly download base, release staleness, maintainer key-person risk |
| `vllm` | 61 | medium | this week running above the monthly average, strong monthly download base, recent vulnerabilities, weak security posture |
| `litellm` | 59 | medium | volatile download pattern, weekly downloads slowing vs prior week, recent vulnerabilities, maintainer key-person risk |
| `langchain` | 53 | medium | weekly downloads accelerating vs prior week, small monthly download base, recent vulnerabilities, weak security posture |
| `langsmith` | 51 | medium | volatile download pattern, strong monthly download base, recent vulnerabilities, maintainer key-person risk |
| `pyautogen` | 23 | low | weekly downloads accelerating vs prior week, small monthly download base, release staleness, issue backlog pressure |

## 📈 Model metrics (held-out)

- **Growth** (LightGBM regressor): Spearman 0.097, MAE 0.163, RMSE 0.282, R² -0.005 · trained on 1568 rows, tested on 392.
- **Risk** (LightGBM classifier): ROC-AUC 0.576 on 40 packages (7 at-risk).

_Metrics are deliberately modest on this small watchlist and improve as daily snapshot history accumulates — they are tracked openly rather than hidden._

## 🛰️ Source coverage

Share of the 40 packages successfully ingested per free public source:

| Source | Coverage |
|---|--:|
| pypistats (downloads) | 100% |
| PyPI JSON (releases) | 100% |
| ecosyste.ms (package) | 100% |
| ecosyste.ms (repo) | 92% |
| deps.dev (+ Scorecard) | 100% |
| OSV.dev (vulnerabilities) | 100% |
| GitHub REST (activity) | 100% |

## 🤖 What the agent crew did

| Agent | Action | Status | Summary |
|---|---|---|---|
| Healer | self_heal_ingest | ok | All sources healthy — no healing needed. |
| DataEngineer | check_ingestion_freshness | ok | Ingested 40 packages. Source success: depsdev 100%, ecosystems_pkg 100%, ecosystems_repo 92%, github 100%, osv 100%, pypi_downloads 100%, pypi_metadata 100% |
| DataQuality | validate_feature_table | ok | 100% download coverage, 0 duplicate(s). Null rates: stars 0%, dependent_repos_count 0%, scorecard_overall 62%, bus_factor 35% |
| DataScientist | retrain_growth_model | ok | Growth model retrained (spearman=0.097, n_train=1568); first champion: spearman=0.097. |
| DataScientist | retrain_risk_model | ok | Risk model retrained (auc=0.576, n_train=40); first champion: auc=0.576 · labels: heuristic. |
| DataScientist | monitor_drift | ok | No prior run to compare — drift baseline established for next run. |
| ImprovementScientist | feature_experiment | ok | Tested 4 candidate features against held-out Spearman: trend_slope_7 Δ+0.042, recent_share Δ-0.009, mom_28v28 Δ-0.011, dow_volatility_7 Δ-0.086. |
| ImprovementScientist | open_pull_request | skipped | Would propose enabling 'trend_slope_7' (Δspearman +0.042); PR skipped (dry-run / no token). |
| RiskAnalyst | write_daily_report | ok | Authored daily brief (template); 40 packages summarized. |
| MLOps | publish_report | ok | Wrote reports/2026-06-21.md |
| MLOps | open_pull_request | skipped | GitHub PR skipped (dry-run or no token). |

_Generated by the OSS Radar agent crew. With an Anthropic key the RiskAnalyst writes a prose brief; in template mode (shown here) it renders this structured report._
