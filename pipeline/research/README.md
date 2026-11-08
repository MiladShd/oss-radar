# Offline download forecast benchmark

This research module does not change production's 70-day model, registry, dashboard, or scheduler. Its 28-day target is deliberately different, so metrics cannot be compared directly with existing production backtests.

`download_protocol_v1.json` was committed in `386f177` before the first benchmark fit. It pins the data fingerprint, model configuration, development and final cutoffs, issuance lag, target, and evaluation rules. The original fit code is also available at that commit. There was no search, clipping, early stopping, or tuning on final periods.

## Reproduce

Use Python 3.12 and the environment versions recorded in the results artifact. Install pipeline dependencies plus the research-only additions:

```bash
python -m pip install -r pipeline/requirements.txt -r pipeline/research/requirements.txt
PYTHONPATH=pipeline python -m oss_radar.research.download_benchmark \
  --data /path/to/frozen/download_history.csv \
  --protocol pipeline/research/download_protocol_v1.json \
  --out /tmp/download-benchmark
python pipeline/scripts/report_download_benchmark.py \
  --results /tmp/download-benchmark --out /tmp/download-benchmark-report
```

The frozen data CSV and its source manifest are included in the deliverable bundle. They can also be exported from the project warehouse with the query in the manifest, but later warehouse revisions may change the hash. The runner refuses a mismatched CSV. Do not silently replace the fingerprint; any changed dataset or protocol constitutes a new experiment.

The optional report renderer describes this fixed experiment; it is not an automatic narrative generator for arbitrary future experiments. The raw metrics and predictions are authoritative.

## Observed result

Across the two final periods, equal-package/equal-period mean absolute log-growth error was:

| Method | Final error | Mean actual top-10 overlap |
|---|---:|---:|
| Seasonal naive | 0.102600 | 4.5 / 10 |
| Damped ETS | 0.204067 | 5.0 / 10 |
| Pooled LightGBM | 0.234550 | 3.0 / 10 |

ETS narrowly won the development baseline comparison, but seasonal naive was best on final magnitude error. LightGBM's final error was 14.9% higher than the development-selected ETS baseline. Its paired package-cluster error-difference interval includes zero and is conditional on only two periods. This does not support promoting either trained candidate or a broad claim of temporal generalization.

All 91 packages were evaluated in each period; no ETS fit required fallback. Structural data checks found 277 complete daily observations per package, no duplicate package-days, nulls, or negative counts. Same-provider reconciliation for vllm, numpy, and feast matched all 84 recent checked daily counts. This does not independently validate historical counts or user adoption.

## Limits and next decision

Historical values may have been revised or backfilled; the one-day availability lag is simulated. The current curated packages are shared between training and test. Final outcomes are limited to two non-overlapping windows, September 5–October 2 and October 3–30, 2026; non-overlap does not guarantee independence.

Do not retune against these final periods after seeing their results. Keep the weekly-repeat forecast as a prospective reference, freeze future inputs and predictions before outcomes begin, and gather more independent evaluation periods. Any future model revision needs a new protocol and new untouched outcomes.
