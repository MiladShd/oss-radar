#!/usr/bin/env bash
# OSS Radar — one-command local demo.
#
# Fresh clone -> visible output. Creates/uses a local venv, installs the pipeline
# and dashboard deps, runs a small dry-run of the full pipeline into a local
# DuckDB warehouse (no cloud, no side effects), then tells you exactly how to
# view the result. Works with no cloud credentials and no Anthropic key.
#
# Usage:
#   scripts/demo_local.sh            # run the demo (8 packages)
#   scripts/demo_local.sh --limit 12 # score more packages
#   scripts/demo_local.sh --serve    # run the demo, then serve the dashboard
#   make demo                        # same as the first form
set -euo pipefail

# --- locate the repo root (this script lives in <repo>/scripts) --------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

LIMIT=8
SERVE=0
PORT=8099
while [ $# -gt 0 ]; do
  case "$1" in
    --limit) LIMIT="${2:?--limit needs a number}"; shift 2 ;;
    --serve) SERVE=1; shift ;;
    --port)  PORT="${2:?--port needs a number}"; shift 2 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

# --- 1) venv -----------------------------------------------------------------
VENV="$REPO_ROOT/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  PY="$(command -v python3.12 || command -v python3 || true)"
  [ -n "$PY" ] || { echo "python3.12 (or python3) not found on PATH" >&2; exit 1; }
  say "Creating virtualenv with $("$PY" --version 2>&1)"
  "$PY" -m venv "$VENV"
fi
PYBIN="$VENV/bin/python"

# --- 2) dependencies (skip if already installed; --reinstall to force) -------
STAMP="$VENV/.oss_radar_demo_installed"
if [ ! -f "$STAMP" ]; then
  say "Installing dependencies (first run — this can take a few minutes)"
  "$PYBIN" -m pip install -q -U pip
  "$PYBIN" -m pip install -q -r pipeline/requirements.txt
  "$PYBIN" -m pip install -q --no-deps -e pipeline
  "$PYBIN" -m pip install -q -r dashboard/requirements.txt
  touch "$STAMP"
else
  note "Dependencies already installed (delete $STAMP to force a reinstall)."
fi

# --- 3) optional GitHub token (purely to lift rate limits) -------------------
if [ -z "${OSS_RADAR_GITHUB_TOKEN:-}" ] && command -v gh >/dev/null 2>&1 \
     && gh auth status >/dev/null 2>&1; then
  OSS_RADAR_GITHUB_TOKEN="$(gh auth token 2>/dev/null || true)"
  export OSS_RADAR_GITHUB_TOKEN
fi
if [ -z "${OSS_RADAR_GITHUB_TOKEN:-}" ]; then
  note "No GitHub token (gh not installed/authenticated). The demo will still"
  note "run; GitHub-derived signals may be rate-limited (HTTP 403). To fix:"
  note "  gh auth login   # then re-run, or set OSS_RADAR_GITHUB_TOKEN=..."
fi

# --- 4) run the pipeline into a predictable local warehouse ------------------
export OSS_RADAR_BACKEND="${OSS_RADAR_BACKEND:-duckdb}"
export OSS_RADAR_DUCKDB_PATH="${OSS_RADAR_DUCKDB_PATH:-$REPO_ROOT/oss_radar.duckdb}"
say "Running the pipeline (dry-run · $LIMIT packages · DuckDB · no cloud, no PRs)"
"$PYBIN" -m oss_radar.cli run --dry-run --limit "$LIMIT"

# --- 5) tell the user where everything is ------------------------------------
say "Done — local artifacts:"
note "warehouse : $OSS_RADAR_DUCKDB_PATH"
LATEST_REPORT="$(find "$REPO_ROOT/reports" -maxdepth 1 -name '*.md' 2>/dev/null | sort | tail -1 || true)"
[ -n "$LATEST_REPORT" ] && note "report    : $LATEST_REPORT"
note "models    : $REPO_ROOT/models_local/"

if [ "$SERVE" = "1" ]; then
  say "Serving the dashboard at http://localhost:$PORT  (Ctrl-C to stop)"
  exec "$VENV/bin/uvicorn" dashboard.app.main:app --port "$PORT"
else
  say "View it:"
  note "source .venv/bin/activate"
  note "uvicorn dashboard.app.main:app --port $PORT   # → http://localhost:$PORT"
  note "…or just:  make dashboard"
fi
