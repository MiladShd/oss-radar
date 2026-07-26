"""OSS Radar dashboard — FastAPI backend serving the SPA and JSON API."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dashboard.app import queries
from oss_radar.audit import audit_packages, fetch_repo_requirements, parse_requirements

log = structlog.get_logger(__name__)
app = FastAPI(title="OSS Radar", docs_url="/api/docs")

STATIC = Path(__file__).parent / "static"


class _ResponseCache:
    """Small process-local TTL cache for read-only dashboard responses."""

    def __init__(self, ttl_seconds: float = 60.0):
        self.ttl_seconds = max(1.0, ttl_seconds)
        self._values: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, loader: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            cached = self._values.get(key)
            if cached and cached[0] > now:
                return cached[1]
        value = loader()
        with self._lock:
            self._values[key] = (now + self.ttl_seconds, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


try:
    _cache_ttl = float(os.environ.get("OSS_RADAR_DASHBOARD_CACHE_TTL", "60"))
except ValueError:
    _cache_ttl = 60.0
_response_cache = _ResponseCache(_cache_ttl)


class _WindowRateLimiter:
    """Small per-instance abuse guard for the public live-audit endpoint."""

    def __init__(self, max_requests: int = 10, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._timestamps and self._timestamps[0] <= now - self.window_seconds:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.max_requests:
                return False
            self._timestamps.append(now)
            return True

    def clear(self) -> None:
        with self._lock:
            self._timestamps.clear()


_audit_limiter = _WindowRateLimiter()


def _safe(fn: Callable[[], Any], default: Any):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        log.warning("api.query_failed", error=str(exc))
        return default


def _cached(key: str, fn: Callable[[], Any], default: Any):
    # Failed queries are not cached, so transient warehouse errors recover on
    # the very next request rather than being pinned for the TTL.
    return _safe(lambda: _response_cache.get(key, fn), default)


@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "git_sha": os.environ.get("GIT_SHA", "unknown"),
        "revision": os.environ.get("K_REVISION", "local"),
    }


@app.get("/api/overview")
def api_overview():
    return JSONResponse(_cached("overview", queries.overview, {
        "data_state": "error",
        "setup_command": "make demo",
        "tracked": 0,
        "movers": [],
        "risks": [],
    }))


@app.get("/api/packages")
def api_packages():
    return JSONResponse(_cached("packages", queries.all_packages, []))


@app.get("/api/package/{name}")
def api_package(name: str):
    normalized = queries.normalize_package_name(name)
    key = f"package:{normalized or 'invalid'}"
    return JSONResponse(_cached(
        key,
        lambda: queries.package_detail(name),
        {"prediction": None, "downloads": [], "snapshots": []},
    ))


@app.get("/api/models")
def api_models():
    return JSONResponse(_cached("models", queries.model_history, []))


@app.get("/api/backtest")
def api_backtest():
    return JSONResponse(_cached("backtest", queries.backtest, {}))


@app.get("/api/agents")
def api_agents():
    return JSONResponse(_cached("agents:80", lambda: queries.agent_activity(80), []))


@app.get("/api/runs")
def api_runs():
    return JSONResponse(_cached("runs:30", lambda: queries.runs(30), []))


@app.get("/api/system-health")
def api_system_health():
    return JSONResponse(_cached("system-health:30", queries.system_health, {
        "data_state": "error", "status": "unknown", "headline": "system health unavailable",
        "run_days": 0, "total_runs": 0, "error_count": 0, "warning_count": 0,
        "logs": [], "issues": [], "runs": [],
    }))


@app.post("/api/audit")
async def api_audit(request: Request):
    try:
        content_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    if content_length > 25_000:
        return JSONResponse({"error": "request body too large"}, status_code=413)
    if not _audit_limiter.allow():
        return JSONResponse(
            {"error": "live audit rate limit exceeded; retry in one minute"},
            status_code=429,
            headers={"Retry-After": "60"},
        )
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 25_000:
            return JSONResponse({"error": "request body too large"}, status_code=413)
        raw.extend(chunk)
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    body = body if isinstance(body, dict) else {}
    text = str(body.get("requirements") or "")[:20_000]
    repo = str(body.get("repo") or "")[:300]
    raw_packages = body.get("packages")
    pkgs = raw_packages[:30] if isinstance(raw_packages, list) else []
    on_demand = bool((body or {}).get("on_demand", True))
    source = None
    if repo:
        deps, source = _safe(lambda: fetch_repo_requirements(repo), ([], None))
    elif text:
        deps = parse_requirements(text)
    else:
        deps = [(p, None) for p in (pkgs or [])]
    if not deps:
        return JSONResponse({"summary": {"total": 0, "audited": 0}, "packages": [],
                             "source": source or "no dependencies found"})
    out = _safe(lambda: audit_packages(deps[:40], on_demand=on_demand, max_on_demand=20),
                {"summary": {}, "packages": [], "error": "audit failed"})
    if source:
        out["source"] = source
    return JSONResponse(out)


@app.get("/api/self-audit")
def api_self_audit():
    return JSONResponse(_cached(
        "self-audit", queries.self_audit, {"summary": {}, "packages": []}
    ))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
