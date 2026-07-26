#!/usr/bin/env python3
"""Reproduce an automated feature PR's lift on the preview warehouse."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from oss_radar.config import get_settings
from oss_radar.features import build_growth_training
from oss_radar.models.experiment import validate_feature_candidate
from oss_radar.warehouse import get_warehouse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, required=True)
    args = parser.parse_args(argv)

    base = json.loads(args.base.read_text())
    head = json.loads(args.head.read_text())
    if base == head:
        print("No active-feature change; independent lift check not applicable.")
        return 0

    settings = get_settings()
    warehouse = get_warehouse(settings)
    history = warehouse.query_df("SELECT name, date, downloads FROM download_history")
    train_df = build_growth_training(history, horizon=settings.growth_horizon_days)
    try:
        result = validate_feature_candidate(
            train_df,
            base,
            head,
            margin=settings.feature_lift_margin,
            seed=settings.random_seed,
        )
    except ValueError as exc:
        print(f"Feature PR validation failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"status": "passed", **(result or {})}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
