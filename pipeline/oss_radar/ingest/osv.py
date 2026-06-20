"""OSV.dev — known vulnerabilities for a package.

Gotchas handled: ecosystem must be exactly 'PyPI'; a clean package returns ``{}`` with no
``vulns`` key; ``published`` (not ``modified``) drives recency windows; severity is often
absent.
"""

from __future__ import annotations

from datetime import UTC, datetime

from oss_radar.ingest.http import HttpClient

URL = "https://api.osv.dev/v1/query"
_SEV_ORDER = {"LOW": 1, "MODERATE": 2, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def _published(v: dict) -> datetime | None:
    ts = v.get("published")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch(client: HttpClient, package: str) -> dict:
    out: dict = {"_ok": False, "vuln_count": 0, "vuln_new_14d": 0, "vuln_new_28d": 0,
                 "max_severity": None}
    data = client.post_json(URL, {"package": {"name": package, "ecosystem": "PyPI"}})
    if data is None:
        return out
    out["_ok"] = True
    vulns = data.get("vulns", [])
    out["vuln_count"] = len(vulns)

    now = datetime.now(UTC)
    worst = 0
    for v in vulns:
        pub = _published(v)
        if pub:
            age = (now - pub).days
            if age <= 14:
                out["vuln_new_14d"] += 1
            if age <= 28:
                out["vuln_new_28d"] += 1
        label = (v.get("database_specific") or {}).get("severity")
        if label:
            worst = max(worst, _SEV_ORDER.get(label.upper(), 0))
    if worst:
        out["max_severity"] = {1: "LOW", 2: "MODERATE", 3: "HIGH", 4: "CRITICAL"}[worst]
    return out
