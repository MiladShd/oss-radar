"""The *active* model feature sets.

The defaults are the base features. The self-improvement agent runs experiments and, when a
candidate feature measurably lifts the held-out metric, opens a PR that edits
``active_features.json`` to enable it. Merging that PR is the only thing that changes which
features the model trains on — so the system proposes its own improvements, but a reviewable
PR (and CI) gates every change to the running model.
"""

from __future__ import annotations

import json
from pathlib import Path

from oss_radar.features.engineering import (
    ALL_DOWNLOAD_FEATURES,
    DOWNLOAD_FEATURES,
    RISK_FEATURES,
)

CONFIG_PATH = Path(__file__).with_name("active_features.json")


def _load() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:  # noqa: BLE001 — fall back to defaults if missing/malformed
        return {}


def active_download_features() -> list[str]:
    cfg = _load().get("download")
    if not cfg:
        return list(DOWNLOAD_FEATURES)
    # only honor features the code actually computes
    return [f for f in cfg if f in ALL_DOWNLOAD_FEATURES] or list(DOWNLOAD_FEATURES)


def active_risk_features() -> list[str]:
    cfg = _load().get("risk")
    if not cfg:
        return list(RISK_FEATURES)
    return [f for f in cfg if f in RISK_FEATURES] or list(RISK_FEATURES)


def with_candidate(candidate: str) -> dict:
    """Return the active-features config with one candidate added to the download set."""
    cfg = _load() or {"download": list(DOWNLOAD_FEATURES), "risk": list(RISK_FEATURES)}
    dl = list(cfg.get("download") or DOWNLOAD_FEATURES)
    if candidate not in dl:
        dl.append(candidate)
    cfg["download"] = dl
    return cfg
