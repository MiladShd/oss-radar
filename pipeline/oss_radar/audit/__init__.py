"""Dependency risk audit — score a user's actual dependencies, not just the watchlist."""
from oss_radar.audit.auditor import audit_packages, parse_requirements

__all__ = ["audit_packages", "parse_requirements"]
