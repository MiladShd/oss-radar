"""Render a PR-preview comment from a dry-run pipeline's DuckDB output."""

from __future__ import annotations

import json
import math
from pathlib import Path

from oss_radar.warehouse import get_warehouse


def reasons(val):
    if isinstance(val, str):
        try:
            return ", ".join(json.loads(val))
        except Exception:  # noqa: BLE001
            return ""
    return ", ".join(val) if isinstance(val, list) else ""


def growth_pred(row):
    return row.get("growth_pred_70d", row.get("growth_pred_7d"))


def scoped_reasons(row, column, legacy_slice):
    if column in row and row.get(column) is not None:
        return reasons(row.get(column))
    legacy = row.get("top_reasons")
    if isinstance(legacy, str):
        try:
            legacy = json.loads(legacy)
        except Exception:  # noqa: BLE001
            legacy = []
    return ", ".join(legacy_slice(legacy)) if isinstance(legacy, list) else ""


def main() -> None:
    wh = get_warehouse()
    preds = wh.query_df(
        "SELECT * FROM predictions WHERE run_id = "
        "(SELECT run_id FROM predictions ORDER BY predicted_at DESC LIMIT 1)"
    )
    lines = ["## 🛰️ OSS Radar — PR preview", "",
             "_This branch ran the full pipeline (ingest → features → train → score → agents) "
             "on a small sample against a local DuckDB warehouse._", ""]
    if preds.empty:
        lines.append("> No predictions produced — check the workflow logs.")
    else:
        mom = preds.sort_values("momentum_score", ascending=False).head(5)
        risk = preds.sort_values("risk_score", ascending=False).head(5)
        lines += [f"**Scored {len(preds)} packages.**", "", "### 🚀 Top momentum",
                  "| Package | Momentum | Δ70d | Why |", "|---|--:|--:|---|"]
        for _, r in mom.iterrows():
            change = math.expm1(float(growth_pred(r)))
            why = scoped_reasons(r, "momentum_reasons", lambda value: value[:2])
            lines.append(
                f"| `{r['name']}` | {r['momentum_score']:.0f} | "
                f"{change:+.1%} | {why} |"
            )
        lines += ["", "### ⚠️ Top risk", "| Package | Risk | Level | Why |", "|---|--:|---|---|"]
        for _, r in risk.iterrows():
            why = scoped_reasons(r, "risk_reasons", lambda value: value[-2:])
            lines.append(
                f"| `{r['name']}` | {r['risk_score']:.0f} | "
                f"{r['risk_level']} | {why} |"
            )
    lines += ["", "<sub>Automated by the OSS Radar PR-preview workflow.</sub>"]
    Path("preview_comment.md").write_text("\n".join(lines))
    print("wrote preview_comment.md")


if __name__ == "__main__":
    main()
