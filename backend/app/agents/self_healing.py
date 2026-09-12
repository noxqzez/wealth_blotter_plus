"""Pydantic self-healing retry wrapper for agent structured outputs."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from backend.app.config import get_settings

logger = structlog.get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

AgentCaller = Callable[[str], Awaitable[str]]


class SelfHealExhaustedError(Exception):
    """Raised when validation still fails after max retries."""

    def __init__(self, message: str, last_error: ValidationError | None = None) -> None:
        super().__init__(message)
        self.last_error = last_error


async def run_with_self_healing(
    *,
    schema: type[T],
    prompt: str,
    caller: AgentCaller,
    max_retries: int | None = None,
) -> T:
    """
    Invoke an LLM/agent caller, parse JSON into `schema`, and on ValidationError
    feed the traceback back into context for up to `max_retries` corrections.
    """
    settings = get_settings()
    retries = (
        max_retries
        if max_retries is not None
        else 3
    )
    # Allow settings / technical_limits to override later via rules engine
    _ = settings

    context = prompt
    last_exc: ValidationError | None = None

    for attempt in range(retries + 1):
        raw = await caller(context)
        try:
            data = _extract_json(raw)
            return schema.model_validate(data)
        except (ValidationError, json.JSONDecodeError, TypeError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                last_exc = exc
                error_text = exc.json()
            else:
                error_text = str(exc)
            logger.warning(
                "agent_output_validation_failed",
                attempt=attempt,
                error=error_text[:2000],
            )
            if attempt >= retries:
                break
            context = (
                f"{prompt}\n\n"
                f"Your previous output failed validation. "
                f"Correct it to match the schema exactly.\n"
                f"Validation error:\n{error_text}\n"
                f"Previous raw output:\n{raw}"
            )

    raise SelfHealExhaustedError(
        f"Failed to produce valid {schema.__name__} after {retries} retries",
        last_error=last_exc,
    )


def _extract_json(raw: str) -> object:
    """Parse JSON object from raw model text (strips optional markdown fences)."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # drop first fence and optional trailing fence
        if lines[-1].strip().startswith("```"):
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines).strip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    return json.loads(text)
