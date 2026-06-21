"""OSS Radar dashboard — FastAPI backend serving the SPA and JSON API."""

from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dashboard.app import queries

log = structlog.get_logger(__name__)
app = FastAPI(title="OSS Radar", docs_url="/api/docs")

STATIC = Path(__file__).parent / "static"


def _safe(fn, default):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        log.warning("api.query_failed", error=str(exc))
        return default


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/overview")
def api_overview():
    return JSONResponse(_safe(queries.overview, {"tracked": 0, "movers": [], "risks": []}))


@app.get("/api/packages")
def api_packages():
    return JSONResponse(_safe(queries.all_packages, []))


@app.get("/api/package/{name}")
def api_package(name: str):
    return JSONResponse(_safe(lambda: queries.package_detail(name), {"prediction": None}))


@app.get("/api/models")
def api_models():
    return JSONResponse(_safe(queries.model_history, []))


@app.get("/api/backtest")
def api_backtest():
    return JSONResponse(_safe(queries.backtest, {}))


@app.get("/api/agents")
def api_agents():
    return JSONResponse(_safe(lambda: queries.agent_activity(80), []))


@app.get("/api/runs")
def api_runs():
    return JSONResponse(_safe(lambda: queries.runs(30), []))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
