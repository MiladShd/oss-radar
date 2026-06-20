"""OSS Radar command-line interface."""

from __future__ import annotations

import argparse
import json
import sys

import structlog

from oss_radar.config import get_settings
from oss_radar.orchestrator import run_pipeline
from oss_radar.warehouse import get_warehouse

structlog.configure(processors=[
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.dev.ConsoleRenderer(),
])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oss-radar", description="OSS Radar pipeline CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the full daily pipeline")
    p_run.add_argument("--dry-run", action="store_true", help="Skip GitHub PR/issue side effects")
    p_run.add_argument("--limit", type=int, default=None, help="Limit watchlist size (debug)")

    sub.add_parser("init-warehouse", help="Create warehouse tables")
    sub.add_parser("info", help="Print resolved configuration")

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

    return 1


if __name__ == "__main__":
    sys.exit(main())
