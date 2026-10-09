"""Dated, reproducible evidence for the claims in the blog that depend on the *current* snapshot.

Usage (repo root):  python docs/blog/charts/build_evidence.py <snapshots.csv> > docs/blog/charts/evidence.json
The CSV is `SELECT * FROM oss_radar.snapshots`. Everything printed is computed here; nothing is typed in.
"""
import hashlib
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, "pipeline")
from oss_radar.models.survival import (  # noqa: E402
    SURVIVAL_FEATURES,
    build_counting_process,
    fit_cox,
    fit_cox_selected,
    score_new_advisory_risk,
)

path = sys.argv[1]
raw = Path(path).read_bytes()
snap = pd.read_csv(path, parse_dates=["snapshot_date", "ingested_at"])
cp = build_counting_process(snap)

s = snap.sort_values(["name", "snapshot_date", "ingested_at"]).drop_duplicates(["name", "snapshot_date"], keep="last")
s["dv"] = s.groupby("name")["vuln_count"].diff()
osv_failed = int(s["source_status"].astype(str).str.contains('"osv": false', case=False).sum())

events_by_day = cp[cp["event"] == 1].groupby("day").size()
origin = s["snapshot_date"].min()
shock_days = [
    {"date": str((origin + pd.Timedelta(days=int(d))).date()), "packages_with_new_advisories": int(n)}
    for d, n in events_by_day.sort_values(ascending=False).head(5).items()
]

selected = fit_cox_selected(cp)
sel = selected.summary()
full = fit_cox(cp)
fsum = full.summary()

out = {
    "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
    "snapshot_rows": int(len(snap)),
    "date_range": [str(s["snapshot_date"].min().date()), str(s["snapshot_date"].max().date())],
    "n_packages": int(s["name"].nunique()),
    "package_days": int(len(cp)),
    "events": int(cp["event"].sum()),
    "event_days": int(events_by_day.shape[0]),
    "count_decreases": int((s["dv"] < 0).sum()),
    "osv_failed_snapshots": osv_failed,
    "biggest_single_day_jump": float(s["dv"].max()),
    "top_shock_days": shock_days,
    "selected_model": {
        "features": selected.features,
        "hazard_ratios_per_sd": {r.feature: round(float(r.hazard_ratio_per_sd), 3) for r in sel.itertuples()},
        "advisory_history_hr_ci": [round(float(x), 3) for x in (sel.iloc[0].hr_ci_low, sel.iloc[0].hr_ci_high)],
    },
    "full_model_hazard_ratios_per_sd": {r.feature: round(float(r.hazard_ratio_per_sd), 3) for r in fsum.itertuples()},
}

scored = score_new_advisory_risk(snap)
if scored is not None:
    probs, info = scored
    p30 = probs["p_new_advisory_30d"]
    out["current_forecast"] = {
        "n_packages": int(len(probs)),
        "median_30d": round(float(p30.median()), 3),
        "at_least_50pct_30d": int((p30 >= 0.5).sum()),
        "at_least_25pct_30d": int((p30 >= 0.25).sum()),
        "below_5pct_30d": int((p30 < 0.05).sum()),
        "recent_baseline_daily_hazard": round(float(info["recent_baseline_daily_hazard"]), 5),
    }

try:  # independent check: Cox (Breslow) == Poisson regression with a free baseline per day
    import statsmodels.api as sm

    nr = fit_cox(cp, list(SURVIVAL_FEATURES), l2=0.0)
    Z = ((cp[nr.features].astype(float).fillna(dict(zip(nr.features, nr.fill, strict=True))) - nr.mean)
         / nr.scale).to_numpy()
    design = np.hstack([Z, pd.get_dummies(cp["day"], dtype=float).to_numpy()])
    glm = sm.GLM(cp["event"].to_numpy(float), design, family=sm.families.Poisson()).fit(maxiter=300, tol=1e-12)
    out["poisson_equivalence_max_abs_coef_diff"] = float(np.abs(nr.beta - glm.params[: len(nr.beta)]).max())
except Exception as exc:  # noqa: BLE001
    out["poisson_equivalence_error"] = str(exc)

json.dump(out, sys.stdout, indent=2)
