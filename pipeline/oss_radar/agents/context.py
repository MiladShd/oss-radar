"""Shared context passed to every agent, plus the activity recorder."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from oss_radar.agents.llm import Claude
from oss_radar.config import Settings


@dataclass
class AgentContext:
    run_id: str
    settings: Settings
    llm: Claude
    dry_run: bool = False
    activities: list[dict] = field(default_factory=list)

    def record(self, agent: str, action: str, status: str, summary: str,
               artifact_url: str = "") -> None:
        self.activities.append(
            {
                "run_id": self.run_id,
                "ts": datetime.now(UTC),
                "agent": agent,
                "action": action,
                "status": status,
                "summary": summary,
                "artifact_url": artifact_url,
            }
        )
