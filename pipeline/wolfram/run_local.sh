#!/usr/bin/env bash
# ============================================================================================
# run_local.sh — daily local driver for the Wolfram growth-model validation cross-check.
#
# 1. Picks the freshest data source (env DATA_DIR > GCS sync > /tmp harness dumps > committed
#    sample_data) so the educational report always reflects the latest available numbers.
# 2. Runs pipeline/wolfram/validate_growth.wl under wolframscript.
# 3. Publishes the step-by-step report to docs/validation_steps.md and refreshes a freshness
#    marker that the cloud staleness-guard watches.
#
# Exit codes: 0 ok | 1 a deterministic cross-check FAILED | 3 wolframscript not installed.
# Wire it up with infra/launchd/com.ossradar.wolfram-validate.plist (macOS) or cron.
# ============================================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WL="$REPO_ROOT/pipeline/wolfram/validate_growth.wl"
SAMPLE="$REPO_ROOT/pipeline/wolfram/sample_data"
LOG_DIR="$REPO_ROOT/pipeline/wolfram/logs"
mkdir -p "$LOG_DIR"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(ts)] $*"; }

# ---- 0) engine present? -------------------------------------------------------------------
if ! command -v wolframscript >/dev/null 2>&1; then
  cat >&2 <<'EOF'
ERROR: `wolframscript` not found.

Install the free Wolfram Engine, then activate it once with your (free) Wolfram ID:
  1. Download:  https://www.wolfram.com/engine/  (Wolfram Engine for Developers, macOS)
  2. Open the .dmg, drag "Wolfram Engine.app" to /Applications, launch it once.
  3. Activate:  wolframscript -activate        # sign in with your Wolfram ID (free)
  4. Verify:    wolframscript -code '1+1'      # -> 2
Then re-run this script.
EOF
  exit 3
fi

# ---- 1) choose data source ----------------------------------------------------------------
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
DATA_DIR="${DATA_DIR:-}"
SRC="unset"

sync_from_gcs() {       # optional: pull the cloud-refreshed authoritative artifacts
  [ -n "${OSS_RADAR_GCS_BUCKET:-}" ] || return 1
  command -v gsutil >/dev/null 2>&1 || return 1
  gsutil -m cp "gs://${OSS_RADAR_GCS_BUCKET}/validation/validation_results.json" \
               "gs://${OSS_RADAR_GCS_BUCKET}/validation/validation_testset.csv" \
               "gs://${OSS_RADAR_GCS_BUCKET}/validation/validation_trainset.csv" \
               "gs://${OSS_RADAR_GCS_BUCKET}/validation/validation_oof.csv" \
               "$WORK/" 2>/dev/null || true
  [ -f "$WORK/validation_testset.csv" ] && [ -f "$WORK/validation_results.json" ]
}

if [ -n "$DATA_DIR" ] && [ -f "$DATA_DIR/validation_testset.csv" ]; then
  SRC="env:$DATA_DIR"
elif sync_from_gcs; then
  DATA_DIR="$WORK"; SRC="gcs:${OSS_RADAR_GCS_BUCKET}"
elif [ -f "/tmp/validation_testset.csv" ] && [ -f "/tmp/validation_results.json" ]; then
  DATA_DIR="/tmp"; SRC="tmp-harness-dumps"
else
  DATA_DIR="$SAMPLE"; SRC="committed-sample_data"
fi
RESULTS_JSON="$DATA_DIR/validation_results.json"
log "data source: $SRC  ($DATA_DIR)"

# ---- 2) run the Wolfram cross-check -------------------------------------------------------
OUT="$WORK/out"; mkdir -p "$OUT"
RUN_LOG="$LOG_DIR/run-$(date -u +%Y%m%d).log"
set +e
wolframscript -file "$WL" "$DATA_DIR" "$RESULTS_JSON" "$OUT" 2>&1 | tee "$RUN_LOG"
STATUS="${PIPESTATUS[0]}"
set -e
log "wolframscript exit: $STATUS"

# ---- 3) publish report + freshness marker -------------------------------------------------
if [ -f "$OUT/validation_steps.md" ]; then
  cp "$OUT/validation_steps.md"    "$REPO_ROOT/docs/validation_steps.md"
  cp "$OUT/wolfram_crosscheck.json" "$REPO_ROOT/docs/wolfram_crosscheck.json"
  cp "$OUT/wolfram_freshness.json"  "$REPO_ROOT/docs/wolfram_freshness.json"
  # keep the dashboard's Validation tab fresh: animated step-by-step JSON + the summary it charts
  [ -f "$OUT/validation_steps.json" ] && cp "$OUT/validation_steps.json" "$REPO_ROOT/dashboard/app/static/validation_steps.json"
  [ -f "$DATA_DIR/validation_results.json" ] && cp "$DATA_DIR/validation_results.json" "$REPO_ROOT/dashboard/app/static/validation.json"
  log "published docs/ report + freshness + dashboard validation_steps.json"
  # optional: push the freshness marker back to GCS so the cloud staleness-guard can see it
  if [ -n "${OSS_RADAR_GCS_BUCKET:-}" ] && command -v gsutil >/dev/null 2>&1; then
    gsutil cp "$OUT/wolfram_freshness.json" \
      "gs://${OSS_RADAR_GCS_BUCKET}/validation/wolfram_freshness.json" 2>/dev/null || true
  fi
else
  log "WARNING: no report produced (engine error?) — see $RUN_LOG"
fi

exit "$STATUS"
