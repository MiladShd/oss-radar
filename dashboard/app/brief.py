"""Decision brief built from observations, with conservative forecast verification."""
from __future__ import annotations

import math

import pandas as pd


def present(value):
    return value is not None and not pd.isna(value)


def sources(row):
    import json
    raw = row.get("source_status")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def observed_brief(current, previous, history):
    """Do not turn unavailable source fields into reassuring zeros or invented changes."""
    old = {r["name"]: r for r in previous}
    findings, changes = [], []
    dates = sorted({str(r["snapshot_date"])[:10] for r in current})
    asof = dates[-1] if dates else None
    coverage = {"downloads": 0, "security": 0}
    series = {}
    for r in history:
        if present(r.get("downloads")):
            series.setdefault(r["name"], {})[str(r["date"])[:10]] = float(r["downloads"])
    available_dates = [d for package in series.values() for d in package if asof and d <= asof]
    download_asof = max(available_dates) if available_dates else None
    for row in current:
        name = row["name"]
        status = sources(row)
        prior = old.get(name)
        osv_ok = status.get("osv") is True
        coverage["security"] += int(osv_ok)
        repo_ok = status.get("github") is True or status.get("ecosystems_repo") is True
        common = {"name": name, "version": row.get("latest_version"), "repo": row.get("repo")}
        if repo_ok and row.get("archived") is True:
            findings.append({**common, "kind": "maintenance", "priority": 1,
                             "evidence": "Repository is archived.",
                             "action": "Review migration options and confirm support requirements.",
                             "source": "Repository metadata"})
        recent = row.get("vuln_new_28d")
        if osv_ok and present(recent) and recent > 0:
            findings.append({**common, "kind": "security", "priority": 0,
                             "evidence": f"{int(recent)} advisory publication(s) in the last 28 days.",
                             "action": "Check your pinned version against OSV before choosing an upgrade.",
                             "source": "OSV package history; installed-version exposure is unverified"})
        days = row.get("days_since_last_release")
        if status.get("pypi_metadata") is True and present(days) and days > 365:
            findings.append({**common, "kind": "maintenance", "priority": 2,
                             "evidence": f"{int(days)} days since the latest release.",
                             "action": "Review maintenance activity; release age alone does not prove abandonment.",
                             "source": "PyPI release metadata"})
        if prior and status.get("pypi_metadata") is True and sources(prior).get("pypi_metadata") is True:
            before, after = prior.get("latest_version"), row.get("latest_version")
            if before and after and before != after:
                changes.append({**common, "kind": "release", "evidence": f"Version changed from {before} to {after}.",
                                "source": "PyPI", "delta": None})
        if not download_asof or status.get("pypi_downloads") is not True:
            continue
        end = pd.Timestamp(download_asof)
        keys = [(end - pd.Timedelta(days=i)).date().isoformat() for i in range(14)]
        values = series.get(name, {})
        if not all(k in values for k in keys):
            continue
        coverage["downloads"] += 1
        now = sum(values[k] for k in keys[:7])
        before = sum(values[k] for k in keys[7:])
        if before <= 0:
            continue
        delta = now / before - 1
        changes.append({**common, "kind": "downloads", "delta": delta,
                        "current": now, "previous": before,
                        "evidence": "Complete 7-day download total versus the preceding 7 days.",
                        "source": "PyPI download history; includes automated installs"})
    return {"as_of": asof, "download_as_of": download_asof, "tracked": len(current), "coverage": coverage,
            "findings": sorted(findings, key=lambda r: (r["priority"], r["name"])),
            "changes": sorted(changes, key=lambda r: abs(r.get("delta") or 0), reverse=True),
            "comparison_date": str(previous[0]["snapshot_date"])[:10] if previous else None}


def forecast_scorecard(predictions, history, asof):
    """Verify stored pre-outcome forecasts on their original target, never a refitted model.

    Exact origin features and issuance before the outcome window are required. Missing dates
    are never filled with zero. Download history may have been revised since issuance.
    """
    result = {"state": "unavailable", "rows": [], "pending": 0, "unverifiable": 0,
              "reason": "No eligible stored forecasts with complete outcome data.",
              "excluded_timing": 0, "excluded_data": 0, "excluded_definition": 0,
              "definition": "70-day growth of a trailing-28-day-smoothed download series",
              "limitations": "One selected origin is descriptive, not independent repeated temporal validation. Download history may be revised."}
    if not asof:
        return result
    cutoff = pd.Timestamp(asof).normalize()
    per_package = {}
    for row in history:
        if present(row.get("downloads")):
            per_package.setdefault(row["name"], {})[pd.Timestamp(row["date"]).normalize()] = float(row["downloads"])
    cohorts = {}
    for row in predictions:
        if row.get("label_version") != "growth-log70d-trailing28-v1":
            result["unverifiable"] += 1
            result["excluded_definition"] += 1
            continue
        if (not present(row.get("feature_date")) or not present(row.get("growth_pred_70d"))
                or not present(row.get("predicted_at"))):
            result["unverifiable"] += 1
            continue
        origin = pd.Timestamp(row["feature_date"]).normalize()
        issued = pd.Timestamp(row["predicted_at"])
        if issued.tzinfo:
            issued = issued.tz_convert("UTC").tz_localize(None)
        # Outcome starts at the next UTC midnight. A forecast issued after that is retrospective.
        if issued >= origin + pd.Timedelta(days=1):
            result["excluded_timing"] += 1
            result["unverifiable"] += 1
            continue
        if origin + pd.Timedelta(days=70) > cutoff:
            result["pending"] += 1
            continue
        daily = per_package.get(row["name"], {})
        index = pd.date_range(origin - pd.Timedelta(days=96), origin + pd.Timedelta(days=70))
        if not all(d in daily for d in index):
            result["excluded_data"] += 1
            result["unverifiable"] += 1
            continue
        smoothed = pd.Series([daily[d] for d in index], index=index).rolling(28, min_periods=28).mean()
        before = smoothed.loc[origin - pd.Timedelta(days=69):origin].sum()
        after = smoothed.loc[origin + pd.Timedelta(days=1):origin + pd.Timedelta(days=70)].sum()
        if before <= 0:
            result["unverifiable"] += 1
            continue
        actual = math.log1p(after) - math.log1p(before)
        pred = float(row["growth_pred_70d"])
        cohorts.setdefault(origin.date().isoformat(), {})[row["name"]] = {
            "name": row["name"], "model_version": row.get("growth_model_version"),
            "issued_at": str(row["predicted_at"]), "origin": origin.date().isoformat(),
            "outcome_date": (origin + pd.Timedelta(days=70)).date().isoformat(),
            "predicted": math.expm1(pred), "actual": math.expm1(actual),
            "log_prediction": pred, "log_actual": actual}
    if not cohorts:
        return result
    origin = max(cohorts)
    rows = list(cohorts[origin].values())
    n = len(rows)
    mae = sum(abs(r["log_prediction"] - r["log_actual"]) for r in rows) / n
    baseline = sum(abs(r["log_actual"]) for r in rows) / n
    k = min(10, n)
    predicted_top = {r["name"] for r in sorted(rows, key=lambda r: r["predicted"], reverse=True)[:k]}
    actual_top = {r["name"] for r in sorted(rows, key=lambda r: r["actual"], reverse=True)[:k]}
    return {**result, "state": "ready", "reason": "Stored forecasts evaluated without retraining.",
            "origin": origin, "packages": n, "origins": 1, "mae_log": mae,
            "baseline_mae_log": baseline, "baseline": "Zero log-growth (no change)",
            "top_k": k, "top_k_hits": len(predicted_top & actual_top),
            "rows": sorted(rows, key=lambda r: abs(r["predicted"]-r["actual"]), reverse=True)}
