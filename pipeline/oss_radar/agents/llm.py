"""Thin Claude wrapper for the agent layer.

Degrades gracefully: if no API key is configured or the call fails, ``generate`` returns
None and callers fall back to deterministic templates, so the pipeline never hard-fails on
the LLM. Uses the official Anthropic SDK (model defaults to claude-opus-4-8).
"""

from __future__ import annotations

import structlog

from oss_radar.config import Settings, get_settings

log = structlog.get_logger(__name__)


class Claude:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._client = None
        if self.settings.use_llm:
            try:
                import anthropic

                self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
            except Exception as exc:  # noqa: BLE001
                log.warning("llm.init_failed", error=str(exc))

    @property
    def available(self) -> bool:
        return self._client is not None

    def generate(self, system: str, prompt: str, max_tokens: int | None = None) -> str | None:
        if not self._client:
            return None
        try:
            resp = self._client.messages.create(
                model=self.settings.llm_model,
                max_tokens=max_tokens or self.settings.llm_max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            return text.strip() or None
        except Exception as exc:  # noqa: BLE001
            log.warning("llm.generate_failed", error=str(exc))
            return None
