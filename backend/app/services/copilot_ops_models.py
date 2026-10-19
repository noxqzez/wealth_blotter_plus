"""Copilot ops query models and small helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from backend.app.core.error_codes import WBP_COPILOT_UNAVAILABLE, new_correlation_id
from backend.app.schemas.portfolio_import import EnrichedPosition
from backend.app.schemas.scenario import ScenarioRunResponse

OpsKind = Literal[
    "download_pdf",
    "latest_portfolio",
    "update_prices",
    "optimized_portfolio",
    "house_research",
    "latest_trades",
    "load_followup",
]

LoadTarget = Literal["scratchpad", "rebalanced", "pdf", "none"]


class OpsQueryMatch(BaseModel):
    kind: OpsKind
    client_alias: str | None = None
    portfolio_id: UUID | None = None
    scenario_run_id: UUID | None = None
    want_load: bool = False
    invalid_portfolio_token: str | None = None


class OpsQueryResult(BaseModel):
    ok: bool
    content: str
    error_code: str | None = None
    correlation_id: str | None = None
    download_base64: str | None = None
    download_filename: str | None = None
    excel_scratchpad_positions: list[EnrichedPosition] = Field(default_factory=list)
    excel_rebalanced: ScenarioRunResponse | None = None
    ops_kind: str | None = None
    ops_portfolio_id: UUID | None = None
    ops_scenario_run_id: UUID | None = None
    ops_client_alias: str | None = None
    ops_load_target: str | None = None


@dataclass
class _ResolvedClient:
    client_id: UUID
    client_alias: str
    portfolio_id: UUID


def _unavailable(message: str = "Request unavailable.") -> OpsQueryResult:
    cid = new_correlation_id()
    short = cid.replace("-", "")[:8]
    return OpsQueryResult(
        ok=False,
        content=f"{message}\n\nRef: `{WBP_COPILOT_UNAVAILABLE}` · `{short}`",
        error_code=WBP_COPILOT_UNAVAILABLE,
        correlation_id=cid,
    )


def _safe_uuid(raw: str | None) -> UUID | None:
    if not raw:
        return None
    try:
        return UUID(str(raw).strip())
    except (ValueError, TypeError, AttributeError):
        return None
