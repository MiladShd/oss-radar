"""Dependency risk audit.

Given a requirements.txt (or a list of names) produce a supply-chain risk report: for each
dependency — a transparent risk score + reasons, vulnerabilities, release staleness, and a
week-over-week download trend, rolled into a plain-English verdict. Watchlist packages are served
instantly from the latest warehouse snapshot; unknown packages are ingested on demand through the
same connectors and scored with the same transparent ``risk_composite`` (no model load), so the
audit works for any PyPI package.

Vulnerabilities are checked VERSION-AWARE: when the requirement pins ``==X.Y.Z`` we ask OSV which
vulns actually affect that version (the real exposure), instead of the package's lifetime CVE count
(which would flag every mature library as "critical").
"""
from __future__ import annotations

import pathlib
import re
import tomllib

import pandas as pd

from oss_radar.config import get_settings
from oss_radar.ingest import github, osv
from oss_radar.ingest.collector import collect_one
from oss_radar.ingest.http import HttpClient
from oss_radar.models.scoring import risk_composite
from oss_radar.warehouse import get_warehouse

_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
_VER = re.compile(r"==\s*([A-Za-z0-9][A-Za-z0-9._!+-]*)")
_SAFE = re.compile(r"^[a-z0-9._-]+$")


def _norm(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def parse_requirements(text: str) -> list[tuple[str, str | None]]:
    """Return (name, pinned_version_or_None) pairs from requirements.txt content."""
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-") or "://" in line:  # flags, -r includes, VCS/URLs
            continue
        spec = line.split(";", 1)[0].strip()  # drop env markers, keep the version spec
        m = _NAME.match(spec)
        if not m:
            continue
        name = _norm(m.group(1))
        if name in seen:
            continue
        seen.add(name)
        vm = _VER.search(spec)
        out.append((name, vm.group(1) if vm else None))
    return out


_GH = re.compile(r"(?:github\.com[/:])?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def _slug(repo: str) -> str | None:
    m = _GH.search(repo.strip())
    return m.group(1) if (m and "/" in m.group(1)) else None


def _parse_pyproject(text: str) -> list[tuple[str, str | None]]:
    try:
        data = tomllib.loads(text)
    except Exception:
        return []
    deps: list[tuple[str, str | None]] = []
    proj = data.get("project", {}) or {}
    for d in proj.get("dependencies", []) or []:
        deps += parse_requirements(d)
    for grp in (proj.get("optional-dependencies") or {}).values():
        for d in grp or []:
            deps += parse_requirements(d)
    poetry = ((data.get("tool", {}) or {}).get("poetry", {}) or {}).get("dependencies", {}) or {}
    for name in poetry:
        if name.lower() != "python":
            deps.append((_norm(name), None))
    seen, out = set(), []
    for n, v in deps:
        if n not in seen:
            seen.add(n)
            out.append((n, v))
    return out


def fetch_repo_requirements(repo: str, settings=None) -> tuple[list[tuple[str, str | None]], str | None]:
    """Fetch a public GitHub repo's dependency list (requirements.txt, then pyproject.toml).

    Returns (deps, source_label). No auth needed — uses raw.githubusercontent.com.
    """
    settings = settings or get_settings()
    slug = _slug(repo)
    if not slug:
        return [], None
    http = HttpClient(timeout=settings.http_timeout)
    paths = ["requirements.txt", "requirements/base.txt", "requirements/requirements.txt", "reqs.txt"]
    for branch in ("main", "master"):
        for path in paths:
            txt = http.get_text(f"https://raw.githubusercontent.com/{slug}/{branch}/{path}")
            if txt:
                deps = parse_requirements(txt)
                if deps:
                    return deps, f"{slug}@{branch}/{path}"
        txt = http.get_text(f"https://raw.githubusercontent.com/{slug}/{branch}/pyproject.toml")
        if txt:
            deps = _parse_pyproject(txt)
            if deps:
                return deps, f"{slug}@{branch}/pyproject.toml"
    return [], f"no dependency file found in {slug}"


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _trend_pct(series: list[float]) -> float | None:
    if len(series) < 14:
        return None
    last7, prior7 = sum(series[-7:]), sum(series[-14:-7])
    if prior7 <= 0:
        return None
    return round((last7 / prior7 - 1.0) * 100, 1)


def _verdict(risk, vuln, sev, dslr, archived, trend, vkind):
    sev = (sev or "").upper()
    if archived is True:
        return "critical", "archived / removed upstream"
    if vkind == "active" and (vuln or 0) > 0 and sev in ("HIGH", "CRITICAL"):
        return "critical", f"pinned version exposed to {int(vuln)} {sev.lower()} vuln" + ("" if vuln == 1 else "s")
    if vkind == "active" and (vuln or 0) > 0:
        return "high", f"pinned version exposed to {int(vuln)} known vuln" + ("" if vuln == 1 else "s")
    if (risk or 0) >= 66:
        return "high", "high composite maintenance risk"
    if dslr and dslr > 365:
        return "watch", f"no release in {int(dslr)} days"
    if trend is not None and trend < -20:
        return "watch", f"downloads down {abs(trend):.0f}% week-over-week"
    if vkind == "historical" and (vuln or 0) > 0:
        return "watch", f"{int(vuln)} historical CVEs — pin a version to check exposure"
    if (risk or 0) >= 40:
        return "watch", "elevated maintenance risk"
    return "healthy", "actively maintained"


def _trends_for_known(wh, exact_names: list[str]) -> dict[str, float | None]:
    safe = [n for n in exact_names if _SAFE.match(_norm(n))]
    if not safe:
        return {}
    inlist = ",".join("'" + n.replace("'", "") + "'" for n in safe)
    try:
        df = wh.query_df(f"SELECT name, date, downloads FROM download_history WHERE name IN ({inlist})")
    except Exception:
        return {}
    out: dict[str, float | None] = {}
    for name, g in df.sort_values("date").groupby("name"):
        out[_norm(name)] = _trend_pct([float(x) for x in g["downloads"]])
    return out


def own_dependencies() -> list[tuple[str, str | None]]:
    """OSS Radar's own pinned dependencies (pipeline + dashboard requirements.txt) — for dogfooding."""
    base = pathlib.Path(__file__).resolve().parents
    candidates = [base[2] / "requirements.txt", base[3] / "dashboard" / "requirements.txt"]
    deps: list[tuple[str, str | None]] = []
    for p in candidates:
        try:
            deps += parse_requirements(p.read_text())
        except Exception:
            continue
    seen, out = set(), []
    for n, v in deps:
        if n not in seen:
            seen.add(n)
            out.append((n, v))
    return out


def audit_own_dependencies(settings=None, on_demand: bool = True) -> dict:
    """Audit OSS Radar's own supply chain (used by the daily pipeline to dogfood the auditor)."""
    return audit_packages(own_dependencies(), settings=settings, on_demand=on_demand)


def audit_packages(deps, settings=None, on_demand: bool = True, max_on_demand: int = 40) -> dict:
    """deps: list of names, or (name, version) pairs (version pins enable active-vuln checks)."""
    settings = settings or get_settings()
    deps = [((d, None) if isinstance(d, str) else (d[0], d[1] if len(d) > 1 else None)) for d in deps]
    wh = get_warehouse(settings)
    try:
        known = wh.query_df("SELECT * FROM snapshots WHERE run_id = "
                            "(SELECT run_id FROM snapshots ORDER BY snapshot_date DESC LIMIT 1)")
        preds = wh.query_df("SELECT * FROM predictions WHERE run_id = "
                            "(SELECT run_id FROM predictions ORDER BY predicted_at DESC LIMIT 1)")
    except Exception:
        known, preds = pd.DataFrame(), pd.DataFrame()
    ks = {_norm(r["name"]): r for _, r in known.iterrows()} if len(known) else {}
    ps = {_norm(r["name"]): r for _, r in preds.iterrows()} if len(preds) else {}
    trends = _trends_for_known(wh, [ks[n]["name"] for n, _ in deps if n in ks]) if ks else {}

    http = gh = None

    def _client():
        nonlocal http, gh
        if http is None:
            http = HttpClient(timeout=settings.http_timeout)
            gh = github.make_client(token=settings.github_token, timeout=settings.http_timeout)
        return http, gh

    fetched = 0
    rows: list[dict] = []
    for name, version in deps:
        reasons: list[str] = []
        risk = trend = None
        if name in ks:
            snap = ks[name]
            source = "watchlist"
            p = ps.get(name)
            if p is not None and _num(p.get("risk_score")) is not None:
                risk = _num(p.get("risk_score"))
                reasons = [str(x) for x in (p.get("top_reasons") or [])][:2]
            else:
                risk, reasons = risk_composite(snap)
            trend = trends.get(name)
        elif on_demand and fetched < max_on_demand:
            h, g = _client()
            fetched += 1
            try:
                res = collect_one({"name": name, "category": "", "repo_override": ""}, h, g, "audit")
                snap = pd.Series(res["snapshot"])
                source = "on-demand"
                risk, reasons = risk_composite(snap)
                hist = sorted(res["history"], key=lambda r: r["date"])
                trend = _trend_pct([float(r["downloads"]) for r in hist])
            except Exception:
                rows.append({"name": name, "version": version, "status": "fetch failed",
                             "source": "on-demand", "verdict": "unknown"})
                continue
        else:
            rows.append({"name": name, "version": version, "verdict": "unknown", "source": None,
                         "status": "not audited (limit reached)" if on_demand else "not in watchlist"})
            continue

        total_vuln = int(_num(snap.get("vuln_count")) or 0)
        sev_total = snap.get("max_severity") if isinstance(snap.get("max_severity"), str) else None
        if version:  # version-aware exposure
            try:
                h, _ = _client()
                ov = osv.fetch(h, name, version=version)
                vuln, sev, vkind = int(ov.get("vuln_count") or 0), ov.get("max_severity"), "active"
            except Exception:
                vuln, sev, vkind = total_vuln, sev_total, "historical"
        else:
            vuln, sev, vkind = total_vuln, sev_total, "historical"

        dslr = _num(snap.get("days_since_last_release"))
        archived = snap.get("archived") is True
        verdict, reason = _verdict(risk, vuln, sev, dslr, archived, trend, vkind)
        rows.append({
            "name": name, "version": version, "source": source, "status": "ok",
            "risk_score": round(risk, 1) if risk is not None else None,
            "vuln_count": vuln, "vuln_kind": vkind, "max_severity": sev,
            "days_since_release": int(dslr) if dslr is not None else None,
            "trend_pct": trend, "verdict": verdict, "reason": reason, "reasons": reasons,
        })

    ok = [r for r in rows if r.get("status") == "ok"]
    summary = {
        "total": len(rows), "audited": len(ok),
        "critical": sum(1 for r in ok if r["verdict"] == "critical"),
        "high": sum(1 for r in ok if r["verdict"] == "high"),
        "watch": sum(1 for r in ok if r["verdict"] == "watch"),
        "healthy": sum(1 for r in ok if r["verdict"] == "healthy"),
        "exposed": sum(1 for r in ok if r.get("vuln_kind") == "active" and (r.get("vuln_count") or 0) > 0),
    }
    rank = {"critical": 0, "high": 1, "watch": 2, "healthy": 3, "unknown": 4}
    rows.sort(key=lambda r: (rank.get(r.get("verdict"), 5), -(r.get("risk_score") or 0)))
    return {"summary": summary, "packages": rows}
