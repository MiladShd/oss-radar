"""Executive evidence must remain honest under missing sources and retrospective records."""
import math

import pandas as pd

from dashboard.app.brief import forecast_scorecard, observed_brief


def test_unknown_security_and_incomplete_downloads_never_become_clean_evidence():
    now = [{"name": "demo", "snapshot_date": "2026-10-31", "vuln_new_28d": 3,
            "archived": True, "days_since_last_release": 500,
            "source_status": {"osv": False, "pypi_downloads": True}}]
    result = observed_brief(now, [], [{"name": "demo", "date": "2026-10-31", "downloads": 10}])
    assert result["coverage"] == {"security": 0, "downloads": 0}
    assert result["findings"] == result["changes"] == []


def test_observed_comparisons_use_complete_raw_windows_and_separate_risk_signals():
    row = {"name": "demo", "snapshot_date": "2026-10-31", "vuln_new_28d": 2,
           "archived": True, "latest_version": "2.0", "source_status": {
               "osv": True, "github": True, "pypi_metadata": True, "pypi_downloads": True}}
    previous = [{**row, "snapshot_date": "2026-10-30", "latest_version": "1.0"}]
    history = [{"name": "demo", "date": d.date().isoformat(), "downloads": 20 if i >= 7 else 10}
               for i, d in enumerate(pd.date_range("2026-10-18", periods=14))]
    result = observed_brief([row], previous, history)
    download = next(c for c in result["changes"] if c["kind"] == "downloads")
    assert (download["current"], download["previous"], download["delta"]) == (140, 70, 1)
    assert [f["kind"] for f in result["findings"]] == ["security", "maintenance"]
    assert any(c["kind"] == "release" for c in result["changes"])


def test_scorecard_uses_frozen_predictions_and_rejects_late_issuance():
    origin = pd.Timestamp("2026-07-01")
    history = [{"name": "demo", "date": d, "downloads": 100}
               for d in pd.date_range(origin-pd.Timedelta(days=96), origin+pd.Timedelta(days=70))]
    record = {"name": "demo", "feature_date": origin, "predicted_at": "2026-07-01T10:00:00Z",
              "label_version": "growth-log70d-trailing28-v1", "growth_pred_70d": math.log(2), "growth_model_version": "frozen-v1"}
    result = forecast_scorecard([record], history, "2026-10-31")
    assert result["state"] == "ready"
    assert result["rows"][0]["predicted"] == 1.0
    assert result["rows"][0]["actual"] == 0.0
    assert result["baseline_mae_log"] == 0.0
    assert result["mae_log"] > result["baseline_mae_log"]
    assert forecast_scorecard([{**record, "predicted_at": "2026-07-02T00:00:00Z"}], history,
                              "2026-10-31")["unverifiable"] == 1
    assert forecast_scorecard([record], history[:-1], "2026-10-31")["state"] == "unavailable"
    assert forecast_scorecard([record], history, "2026-07-15")["pending"] == 1


def test_executive_api_and_explorer_remain_separate(tmp_path):
    from fastapi.testclient import TestClient

    from dashboard.app import main, queries
    from dashboard.tests.test_api_smoke import _seed

    wh = _seed(str(tmp_path / "brief.duckdb"))
    prior = queries._wh_cache
    queries._wh_cache = wh
    main._response_cache.clear()
    try:
        client = TestClient(main.app)
        response = client.get("/api/brief")
        assert response.status_code == 200
        assert response.json()["data_state"] == "ready"
        assert "Can download history predict" in client.get("/").text
        assert "What the operational agents did" in client.get("/explorer").text
    finally:
        queries._wh_cache = prior
        main._response_cache.clear()
        wh.close()
