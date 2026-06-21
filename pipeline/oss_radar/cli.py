"""OSS Radar command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import structlog

from oss_radar.config import get_settings
from oss_radar.orchestrator import run_pipeline
from oss_radar.warehouse import get_warehouse

structlog.configure(processors=[
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.dev.ConsoleRenderer(),
])

log = structlog.get_logger(__name__)

# Artifacts the validation harness emits; the Wolfram cross-check + dashboard consume them.
_VALIDATION_ARTIFACTS = (
    "validation_results.json", "validation_testset.csv",
    "validation_trainset.csv", "validation_oof.csv",
)


def _run_validation(settings, out_dir: str, upload: bool, staleness_hours: float) -> dict:
    """Cloud-side backstop for the local Wolfram cross-check: regenerate the validation
    statistics + reproducibility dumps, upload them to GCS, and alarm if the local Wolfram
    educational report has gone stale. The numbers themselves stay fresh even if the local
    Mac (which holds the Wolfram Engine) is offline."""
    import oss_radar

    script = Path(oss_radar.__file__).resolve().parent.parent / "scripts" / "validate_growth.py"
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "VALIDATION_OUT": str(out / "validation_results.json"),
           "VALIDATION_ARTIFACT_DIR": str(out)}
    log.info("validate.start", script=str(script), out=str(out))
    subprocess.run([sys.executable, str(script)], env=env, check=True)

    present = [a for a in _VALIDATION_ARTIFACTS if (out / a).exists()]
    _check_wolfram_staleness(settings, out, staleness_hours)
    if upload:
        _upload_validation(settings, out, present)
    return {"out": str(out), "artifacts": present}


def _upload_validation(settings, out: Path, artifacts: list[str]) -> None:
    try:
        from google.cloud import storage

        client = storage.Client(project=settings.gcp_project)
        bucket = client.bucket(settings.artifact_bucket)
        if not bucket.exists():
            bucket = client.create_bucket(bucket, location=settings.region)
        for a in artifacts:
            bucket.blob(f"validation/{a}").upload_from_filename(str(out / a))
        log.info("validate.uploaded", bucket=settings.artifact_bucket, n=len(artifacts))
    except Exception as exc:  # noqa: BLE001
        log.warning("validate.upload_failed", error=str(exc))


def _check_wolfram_staleness(settings, out: Path, hours: float) -> None:
    """The local daily Wolfram run publishes wolfram_freshness.json (also pushed to GCS). If it
    is older than `hours`, the educational cross-check has not refreshed — warn, but the cloud
    numbers remain authoritative."""
    marker = out / "wolfram_freshness.json"
    if not marker.exists():
        try:  # best-effort fetch from GCS so this works inside the Cloud Run job
            from google.cloud import storage

            client = storage.Client(project=settings.gcp_project)
            blob = client.bucket(settings.artifact_bucket).blob("validation/wolfram_freshness.json")
            if blob.exists():
                blob.download_to_filename(str(marker))
        except Exception:  # noqa: BLE001
            pass
    if not marker.exists():
        log.warning("validate.wolfram_marker_missing",
                    note="local Wolfram cross-check has not published a freshness marker yet")
        return
    try:
        raw = json.loads(marker.read_text())["last_run"].replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:  # tolerate a tz-naive stamp by treating it as UTC
            dt = dt.replace(tzinfo=UTC)
        age_h = (datetime.now(UTC) - dt).total_seconds() / 3600
        if age_h > hours:
            log.warning("validate.wolfram_stale", age_hours=round(age_h, 1), threshold_hours=hours,
                        note="local Wolfram educational cross-check is stale; cloud numbers authoritative")
        else:
            log.info("validate.wolfram_fresh", age_hours=round(age_h, 1))
    except Exception as exc:  # noqa: BLE001
        log.warning("validate.staleness_check_failed", error=str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oss-radar", description="OSS Radar pipeline CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the full daily pipeline")
    p_run.add_argument("--dry-run", action="store_true", help="Skip GitHub PR/issue side effects")
    p_run.add_argument("--limit", type=int, default=None, help="Limit watchlist size (debug)")

    sub.add_parser("init-warehouse", help="Create warehouse tables")
    sub.add_parser("info", help="Print resolved configuration")

    p_val = sub.add_parser("validate", help="Regenerate growth-model validation stats + dumps "
                                            "(cloud backstop for the local Wolfram cross-check)")
    p_val.add_argument("--out", default="/tmp/validation", help="artifact output directory")
    p_val.add_argument("--upload", action="store_true", help="upload artifacts to GCS validation/")
    p_val.add_argument("--staleness-hours", type=float, default=36.0,
                       help="warn if the local Wolfram freshness marker is older than this")

    p_gate = sub.add_parser("gate", help="Run the validation gate on the warehouse's growth data "
                                         "(CI/PR merge guard); --require-pass exits nonzero on failure")
    p_gate.add_argument("--require-pass", action="store_true",
                        help="exit 1 if the gate FAILS (a skip on thin data still exits 0)")

    p_audit = sub.add_parser("audit", help="Audit dependencies for supply-chain risk")
    p_audit.add_argument("-r", "--requirements", help="path to a requirements.txt")
    p_audit.add_argument("--repo", help="GitHub repo (owner/repo or URL) to fetch dependencies from")
    p_audit.add_argument("--packages", help="comma-separated package names")
    p_audit.add_argument("--no-fetch", action="store_true",
                         help="warehouse only; do not ingest unknown packages live")
    p_audit.add_argument("--json", action="store_true", help="emit JSON instead of a table")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "info":
        print(json.dumps({
            "env": settings.env, "backend": settings.backend, "gcp_project": settings.gcp_project,
            "bq_dataset": settings.bq_dataset, "artifact_bucket": settings.artifact_bucket,
            "github_repo": settings.github_repo, "use_llm": settings.use_llm,
            "llm_model": settings.llm_model, "watchlist_limit": settings.watchlist_limit,
        }, indent=2))
        return 0

    if args.command == "init-warehouse":
        get_warehouse(settings).init_schema()
        print("warehouse schema ready")
        return 0

    if args.command == "run":
        if args.limit is not None:
            settings.watchlist_limit = args.limit
        result = run_pipeline(settings, dry_run=args.dry_run)
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.command == "validate":
        result = _run_validation(settings, args.out, args.upload, args.staleness_hours)
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.command == "gate":
        return _run_gate(settings, args.require_pass)

    if args.command == "audit":
        return _run_audit(settings, args)

    return 1


def _run_audit(settings, args) -> int:
    from oss_radar.audit import audit_packages, fetch_repo_requirements, parse_requirements

    source = None
    if args.repo:
        deps, source = fetch_repo_requirements(args.repo, settings)
    elif args.requirements:
        deps = parse_requirements(Path(args.requirements).read_text())
        source = args.requirements
    elif args.packages:
        deps = [(p.strip().lower().replace("_", "-"), None) for p in args.packages.split(",") if p.strip()]
    else:
        print("provide -r FILE, --repo OWNER/REPO, or --packages a,b,c", file=sys.stderr)
        return 2
    if not deps:
        print(f"no dependencies found ({source})", file=sys.stderr)
        return 2

    rep = audit_packages(deps, settings=settings, on_demand=not args.no_fetch)
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
        return 0
    s = rep["summary"]
    if source:
        print(f"source: {source}")
    print(f"\n{s.get('audited', 0)} of {s.get('total', 0)} audited — "
          f"{s.get('critical', 0)} critical, {s.get('high', 0)} high, "
          f"{s.get('watch', 0)} watch, {s.get('healthy', 0)} healthy")
    if s.get("exposed"):
        print(f"  {s['exposed']} pinned version(s) exposed to ACTIVE CVEs")
    icon = {"critical": "!!", "high": " !", "watch": " ~", "healthy": " ok", "unknown": " ?"}
    print()
    for r in rep["packages"]:
        if r.get("status") != "ok":
            print(f"  {icon.get(r.get('verdict'), '  '):>3} {r['name']:24s} {r.get('status')}")
            continue
        nm = r["name"] + (f"=={r['version']}" if r.get("version") else "")
        vuln = f"{r['vuln_count']} {r['vuln_kind']}" if r.get("vuln_count") else "-"
        print(f"  {icon.get(r['verdict'], '  '):>3} {nm:24s} risk {str(r.get('risk_score')):>5}  "
              f"vuln {vuln:13s}  {r.get('reason', '')}")
    return 0


def _run_gate(settings, require_pass: bool) -> int:
    """Run the growth validation gate against the warehouse's download history. Used as a CI/PR
    merge guard: with --require-pass it exits 1 on a genuine gate failure (a skip on insufficient
    data exits 0 — we never block when there isn't enough data to verify anything)."""
    from oss_radar.config.active_features import active_download_features
    from oss_radar.features import build_growth_training
    from oss_radar.models.validation_gate import growth_gate

    hist = get_warehouse(settings).query_df("SELECT name, date, downloads FROM download_history")
    train_df = build_growth_training(hist)
    gate = growth_gate(train_df, active_download_features(), settings)
    print(json.dumps({"passed": gate.passed, "skipped": gate.skipped,
                      "reasons": gate.reasons, "metrics": gate.metrics}, indent=2, default=float))
    if require_pass and not gate.passed and not gate.skipped:
        log.error("gate.failed", reasons=gate.reasons)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
