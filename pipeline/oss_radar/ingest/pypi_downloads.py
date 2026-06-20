"""pypistats.org — daily download series and derived velocity/acceleration.

Verified behavior: /overall returns ~180 days x {with_mirrors, without_mirrors}; the
series is NOT contiguous (global gaps occur), so windows are summed by CALENDAR date,
treating missing days as 0, rather than by row index.
"""

from __future__ import annotations

from datetime import date, timedelta

from oss_radar.ingest.http import HttpClient

BASE = "https://pypistats.org/api/packages"


def _calendar_sum(series: dict[date, int], end: date, days: int, offset: int = 0) -> int:
    last = end - timedelta(days=offset)
    first = last - timedelta(days=days - 1)
    return sum(v for d, v in series.items() if first <= d <= last)


def fetch(client: HttpClient, package: str) -> dict:
    """Return download metrics + the raw daily series (for history backfill)."""
    out: dict = {"_ok": False, "history": []}
    data = client.get_json(f"{BASE}/{package.lower()}/overall")
    if not data or "data" not in data:
        return out

    series: dict[date, int] = {}
    history = []
    for row in data["data"]:
        if row.get("category") != "without_mirrors":
            continue
        try:
            d = date.fromisoformat(row["date"])
        except (ValueError, KeyError):
            continue
        dl = int(row.get("downloads") or 0)
        series[d] = dl
        history.append({"name": package, "date": d, "downloads": dl})

    if not series:
        return out

    last = max(series)
    d7 = _calendar_sum(series, last, 7)
    prev7 = _calendar_sum(series, last, 7, offset=7)
    out.update(
        {
            "_ok": True,
            "downloads_1d": series.get(last, 0),
            "downloads_7d": d7,
            "downloads_28d": _calendar_sum(series, last, 28),
            "download_velocity": round(d7 / 7.0, 2),
            "download_acceleration": d7 - prev7,
            "history": history,
        }
    )
    return out
