"""OSV.dev — known vulnerabilities for a package.

Gotchas handled: ecosystem must be exactly 'PyPI'; a clean package returns ``{}`` with no
``vulns`` key; ``published`` (not ``modified``) drives recency windows; severity is often
absent.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cvss import CVSS2, CVSS3, CVSS4
from cvss.exceptions import CVSSError

from oss_radar.ingest.http import HttpClient

URL = "https://api.osv.dev/v1/query"
_SEV_ORDER = {"LOW": 1, "MODERATE": 2, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
_SEV_FROM_ORDER = {1: "LOW", 2: "MODERATE", 3: "HIGH", 4: "CRITICAL"}


def _published(v: dict) -> datetime | None:
    ts = v.get("published")
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _severity_label(vulnerability: dict) -> str | None:
    """Resolve OSV database labels or CVSS vectors into one normalized severity band."""
    labels: list[str] = []
    database_label = (vulnerability.get("database_specific") or {}).get("severity")
    if isinstance(database_label, str):
        labels.append(database_label.upper())

    for item in vulnerability.get("severity") or []:
        score = item.get("score")
        if not isinstance(score, str) or not score:
            continue
        try:
            numeric = float(score)
            label = (
                "CRITICAL" if numeric >= 9
                else "HIGH" if numeric >= 7
                else "MODERATE" if numeric >= 4
                else "LOW"
            )
        except ValueError:
            try:
                if score.startswith("CVSS:4"):
                    label = CVSS4(score).severities()[0].upper()
                elif score.startswith("CVSS:3"):
                    label = CVSS3(score).severities()[0].upper()
                else:
                    label = CVSS2(score).severities()[0].upper()
            except (CVSSError, KeyError, TypeError, ValueError):
                continue
        labels.append(label)

    worst = max((_SEV_ORDER.get(label, 0) for label in labels), default=0)
    return _SEV_FROM_ORDER.get(worst)


def fetch(client: HttpClient, package: str, version: str | None = None) -> dict:
    """Vulnerabilities for a package. If ``version`` is given, OSV returns only the vulns that
    actually affect that version — the right signal for auditing a pinned dependency."""
    out: dict = {"_ok": False, "vuln_count": 0, "vuln_new_14d": 0, "vuln_new_28d": 0,
                 "max_severity": None, "max_severity_new_28d": None}
    payload: dict = {"package": {"name": package, "ecosystem": "PyPI"}}
    if version:
        payload["version"] = version
    data = client.post_json(URL, payload)
    if data is None:
        return out
    out["_ok"] = True
    vulns = data.get("vulns", [])
    out["vuln_count"] = len(vulns)

    now = datetime.now(UTC)
    worst = 0
    worst_recent_28d = 0
    for v in vulns:
        pub = _published(v)
        age = None
        if pub:
            age = (now - pub).days
            if 0 <= age <= 14:
                out["vuln_new_14d"] += 1
            if 0 <= age <= 28:
                out["vuln_new_28d"] += 1
        label = _severity_label(v)
        if label:
            severity = _SEV_ORDER.get(label, 0)
            worst = max(worst, severity)
            if age is not None and 0 <= age <= 28:
                worst_recent_28d = max(worst_recent_28d, severity)
    if worst:
        out["max_severity"] = _SEV_FROM_ORDER[worst]
    if worst_recent_28d:
        out["max_severity_new_28d"] = _SEV_FROM_ORDER[worst_recent_28d]
    return out
