# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM access for the agentic workflows.

Only the :class:`LLM` protocol is used by the workflows, so tests (and other
providers) can supply their own. :class:`ClaudeLLM` talks to Claude through the
official ``anthropic`` SDK, which is an *optional* dependency
(``pip install "rules-requirements[agents]"``): without it the deterministic
workflows still run and the LLM-backed ones report themselves unavailable.

Requests use adaptive thinking, JSON-schema structured output, streaming (the
inputs can be large source excerpts) and the server-side refusal fallback
(``fallbacks: "default"``), and check ``stop_reason`` before reading content.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """The model could not produce a usable answer."""


class LLMUnavailable(LLMError):  # noqa: N818 — reads better than LLMUnavailableError
    """No LLM is configured (SDK missing, or disabled)."""


class LLM(Protocol):
    name: str

    def json(self, system: str, prompt: str, schema: dict[str, Any]) -> Any:
        """Answer ``prompt`` with a JSON value conforming to ``schema``."""
        ...


class ClaudeLLM:
    """Claude via the ``anthropic`` SDK."""

    def __init__(self, model: str = "", effort: str = "", max_tokens: int = 64000, client: Any = None):
        self.model = model or os.environ.get("RR_AGENT_MODEL") or DEFAULT_MODEL
        self.effort = effort or os.environ.get("RR_AGENT_EFFORT") or DEFAULT_EFFORT
        self.max_tokens = max_tokens
        if client is None:
            try:
                import anthropic  # type: ignore[import-not-found,unused-ignore]
            except ImportError as exc:
                raise LLMUnavailable(
                    'the "anthropic" package is not installed (pip install "rules-requirements[agents]")'
                ) from exc
            # Credentials resolve from the environment (ANTHROPIC_API_KEY,
            # ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile).
            client = anthropic.Anthropic()
        self.client = client
        self.name = f"{self.model} (effort {self.effort})"

    def json(self, system: str, prompt: str, schema: dict[str, Any]) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
            # On a safety-classifier decline, re-run server-side on the model
            # Anthropic recommends for that refusal category.
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        try:
            with self.client.beta.messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
        except Exception as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMError(f"the model declined this request (refusal category: {category})")
        if message.stop_reason == "max_tokens":
            raise LLMError("the answer was truncated (max_tokens)")
        text = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"the model returned invalid JSON: {exc}") from exc


def default_llm(enabled: bool = True, model: str = "", effort: str = "") -> LLM | None:
    """A :class:`ClaudeLLM` if possible, else ``None`` (``llm_status`` says why)."""
    if not enabled:
        return None
    try:
        return ClaudeLLM(model=model, effort=effort)
    except LLMUnavailable:
        return None


def llm_status(llm: LLM | None, enabled: bool = True) -> dict[str, Any]:
    if llm is not None:
        return {"available": True, "name": llm.name}
    if not enabled:
        return {"available": False, "reason": "LLM workflows are disabled (--no-llm)"}
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return {
            "available": False,
            "reason": 'install the optional "anthropic" package: pip install "rules-requirements[agents]"',
        }
    return {"available": False, "reason": "the Claude client could not be created"}
