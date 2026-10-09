"""Re-derive the numbers the blog charts plot, from live warehouse exports, with the production code.

Usage (from repo root):  python docs/blog/charts/build_data.py <download_history.csv> > docs/blog/charts/data.json
The CSV is `SELECT * FROM oss_radar.download_history`. Conformal figures use the production feature set.
Survival figures are read from docs/survival_results.json by make_charts.py, not recomputed here.
"""
import json
import math
import sys
import warnings

import lightgbm as lgb
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, "pipeline")
from oss_radar.config.active_features import active_download_features  # noqa: E402
from oss_radar.features import GROWTH_TARGET_COLUMN, build_growth_training  # noqa: E402
from oss_radar.models.conformal import forward_chain  # noqa: E402
from oss_radar.models.evaluation import date_grouped_train_validation_test  # noqa: E402
from oss_radar.models.growth import _MAX_ESTIMATORS, _TARGET_MAX, _TARGET_MIN, GrowthModel  # noqa: E402

hist = pd.read_csv(sys.argv[1], parse_dates=["date"])
df = build_growth_training(hist, horizon=70).dropna(subset=[GROWTH_TARGET_COLUMN])
features = active_download_features()
split = date_grouped_train_validation_test(df)
tr, va, te = split.train, split.validation, split.test
model = GrowthModel(features=features)._new_model(_MAX_ESTIMATORS)
clip = lambda s: s.clip(_TARGET_MIN, _TARGET_MAX)  # noqa: E731
model.fit(tr[features].astype(float), clip(tr[GROWTH_TARGET_COLUMN]),
          eval_set=[(va[features].astype(float), clip(va[GROWTH_TARGET_COLUMN]))],
          eval_metric="l1", callbacks=[lgb.early_stopping(40, verbose=False)])
pool = pd.concat([va, te], ignore_index=True)
err = pool[GROWTH_TARGET_COLUMN].to_numpy(float) - model.predict(pool[features].astype(float))
dates = pd.to_datetime(pool["feature_date"]).dt.date
by = pd.DataFrame({"date": dates, "abs": np.abs(err), "err": err}).groupby("date").agg(
    mae=("abs", "mean"), bias=("err", "mean"), n=("abs", "size")).reset_index()

rv = np.abs(va[GROWTH_TARGET_COLUMN].to_numpy(float) - model.predict(va[features].astype(float)))
rt = np.abs(te[GROWTH_TARGET_COLUMN].to_numpy(float) - model.predict(te[features].astype(float)))
r = np.abs(err)
di = pd.factorize(pd.to_datetime(pool["feature_date"]), sort=True)[0]
out = {"data_through": str(hist["date"].max().date()), "n_rows": int(len(df)),
       "n_packages": int(df["name"].nunique()), "n_origin_dates": int(df["feature_date"].nunique()),
       "n_features": len(features),
       "error_by_date": [{"date": str(d), "mae": float(m), "bias": float(b), "n": int(n)}
                         for d, m, b, n in by.itertuples(index=False)],
       "coverage": {}}
for a in (0.2, 0.1):
    k = math.ceil((len(rv) + 1) * (1 - a))
    single = float(np.mean(rt <= np.sort(rv)[k - 1]))
    rows = {"single_split": {"coverage": single}}
    for key, hl, gm in (("plain", 1e9, 0.0), ("recency", 2.0, 0.0), ("aci", 2.0, 0.2)):
        res = forward_chain(r, di, alpha=a, half_life=hl, gamma=gm)
        rows[key] = {"coverage": res.coverage, "width": res.mean_width,
                     "dates_scored": res.n_dates_scored, "rows_scored": res.n_scored}
    out["coverage"][f"{int((1 - a) * 100)}"] = rows
json.dump(out, sys.stdout, indent=2)
