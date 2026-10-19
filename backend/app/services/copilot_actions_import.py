"""Copilot import-path actions: validate, drop-invalid, enrich."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import structlog

from backend.app.core.error_codes import (
    WBP_IMPORT_BUILD,
    WBP_IMPORT_CLIENT,
    WBP_IMPORT_VALIDATE,
    WBP_SCENARIO_CLIENT,
)
from backend.app.schemas.copilot import (
    CopilotAgentResponse,
    CopilotSessionState,
    GuardrailCheck,
    WorkflowState,
)
from backend.app.schemas.portfolio_import import (
    BuildBaseRequest,
    BuildBaseResponse,
    ImportValidationRequest,
    RawImportRow,
)
from backend.app.schemas.rules import TechnicalLimits
from backend.app.services import copilot_session as sessions
from backend.app.services.copilot_common import (
    ACTION_DROP,
    ACTION_ENRICH,
    ACTION_PROCESS,
    ACTION_REPROCESS,
    CopilotFsmError,
    _TICKER_IN_ERROR_RE,
    _action_drop,
    _action_enrich,
    _action_process,
    _action_reprocess,
    _placeholder_for,
    _response,
)
from backend.app.services.copilot_excel_writes import (
    SCRATCHPAD_SHEET,
    build_scratchpad_write,
)
from backend.app.services.portfolio_import import (
    build_base,
    set_portfolio_import_hitl,
    validate_import,
)
from backend.app.services.portfolio_normalize import ImportValidationError

logger = structlog.get_logger(__name__)

def _rows_from_payload(rows: list[dict[str, Any]] | None) -> list[RawImportRow]:
    if not rows:
        raise CopilotFsmError(
            "Excel selection rows are required for this action.",
            error_code=WBP_IMPORT_CLIENT,
            http_status=400,
        )
    try:
        return [RawImportRow.model_validate(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        raise CopilotFsmError(
            f"Invalid import rows: {exc}",
            error_code=WBP_IMPORT_CLIENT,
            http_status=400,
        ) from exc


def _extract_invalid_tickers(error_message: str) -> list[str]:
    found = _TICKER_IN_ERROR_RE.findall(error_message or "")
    # Also catch "for TICKER" style
    extra = re.findall(
        r"(?:for ticker|ticker)\s+([A-Za-z0-9.\-]+)",
        error_message or "",
        flags=re.IGNORECASE,
    )
    tickers: list[str] = []
    for t in [*found, *extra]:
        u = t.strip().upper()
        if u and u not in tickers:
            tickers.append(u)
    return tickers


async def _validate_rows(
    rows: list[RawImportRow],
    *,
    state: CopilotSessionState,
    source_sheet_name: str | None,
    limits: TechnicalLimits | None = None,
) -> CopilotAgentResponse:
    payload = ImportValidationRequest(rows=rows)
    try:
        result = await validate_import(payload, advisor_id=state.advisor_id)
    except ImportValidationError as exc:
        err = str(exc)
        state.current_state = WorkflowState.BLOTTER_HAS_ERRORS
        state.last_validation_error = err
        state.last_import_rows = [r.model_dump(mode="json") for r in rows]
        if source_sheet_name:
            state.last_source_sheet = source_sheet_name
        state.pending_action_ids = [ACTION_REPROCESS, ACTION_DROP]
        sessions.save_session(state)
        return _response(
            state,
            content=(
                "### Blotter validation errors\n\n"
                f"{err}\n\n"
                "Fix the list in Excel and **Reprocess**, or try "
                "**Drop Invalid and Proceed** if the error names specific tickers."
            ),
            actions=[_action_reprocess(), _action_drop()],
            guardrails=[
                GuardrailCheck(
                    rule_id="copilot.blotter_validation",
                    severity="blocking_error",
                    message=err,
                    suggested_fix="Correct tickers / quantities in the sheet.",
                )
            ],
            badges=["BLOTTER_HAS_ERRORS", WBP_IMPORT_VALIDATE],
            accept_user_text=False,
        )

    state.current_state = WorkflowState.BLOTTER_VALIDATED
    state.portfolio_id = result.portfolio_id
    state.draft_id = result.draft_id
    state.last_validation_error = None
    state.last_import_rows = [r.model_dump(mode="json") for r in rows]
    if source_sheet_name:
        state.last_source_sheet = source_sheet_name
    state.pending_action_ids = [ACTION_ENRICH]
    sessions.save_session(state)

    try:
        set_portfolio_import_hitl(result.portfolio_id, hitl=bool(state.import_hitl))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "portfolio_import_hitl_persist_failed",
            portfolio_id=str(result.portfolio_id),
            import_hitl=bool(state.import_hitl),
            error=str(exc),
        )

    # Clean first-pass validation: enrich immediately (no extra click).
    if not state.import_hitl:
        enrich_resp = await _handle_enrich(state, limits=limits)
        notes = "\n".join(f"- {m}" for m in result.messages) if result.messages else ""
        prefix_parts = [
            f"Blotter validated for **{result.client_alias}** "
            f"({len(result.positions)} position(s)). Auto-enriching…",
        ]
        if notes:
            prefix_parts.append(notes)
        prefix = "\n".join(prefix_parts) + "\n\n"
        return enrich_resp.model_copy(update={"content": prefix + enrich_resp.content})

    notes = "\n".join(f"- {m}" for m in result.messages) if result.messages else ""
    parts = [
        f"Blotter validated for **{result.client_alias}** "
        f"({len(result.positions)} position(s)).",
    ]
    if notes:
        parts.append(notes)
    parts.append(
        f"Next: **Enrich Portfolio** to pull market data into `{SCRATCHPAD_SHEET}`."
    )
    parts.append("Import HITL flagged (reprocess / drop invalid).")
    return _response(
        state,
        content="\n\n".join(parts),
        actions=[_action_enrich()],
        badges=["BLOTTER_VALIDATED", "IMPORT_HITL"],
        accept_user_text=False,
    )

async def _handle_drop_invalid(
    state: CopilotSessionState,
    *,
    limits: TechnicalLimits | None = None,
) -> CopilotAgentResponse:
    err = state.last_validation_error or ""
    tickers = _extract_invalid_tickers(err)
    raw_rows = state.last_import_rows or []
    if not tickers or not raw_rows:
        state.pending_action_ids = [ACTION_REPROCESS, ACTION_DROP]
        sessions.save_session(state)
        return _response(
            state,
            content=(
                "I couldn't identify specific invalid tickers from the last error "
                f"({err or 'unknown'}).\n\n"
                "Please fix the sheet in Excel and click **Reprocess Corrected List**."
            ),
            actions=[_action_reprocess(), _action_drop()],
            badges=["BLOTTER_HAS_ERRORS"],
            accept_user_text=False,
        )

    drop_set = {t.upper() for t in tickers}
    # Also drop crypto bases without -USD suffix match
    filtered: list[dict[str, Any]] = []
    for row in raw_rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        base = ticker.removesuffix("-USD")
        if ticker in drop_set or base in drop_set:
            continue
        filtered.append(row)

    if len(filtered) == len(raw_rows):
        state.pending_action_ids = [ACTION_REPROCESS, ACTION_DROP]
        sessions.save_session(state)
        return _response(
            state,
            content=(
                f"No rows matched invalid tickers ({', '.join(tickers)}). "
                "Fix the sheet and **Reprocess**."
            ),
            actions=[_action_reprocess(), _action_drop()],
            badges=["BLOTTER_HAS_ERRORS"],
            accept_user_text=False,
        )

    if not filtered:
        state.pending_action_ids = [ACTION_REPROCESS, ACTION_DROP]
        sessions.save_session(state)
        return _response(
            state,
            content=(
                "Dropping those tickers would leave an empty list. "
                "Please correct the sheet and **Reprocess**."
            ),
            actions=[_action_reprocess(), _action_drop()],
            badges=["BLOTTER_HAS_ERRORS"],
            accept_user_text=False,
        )

    logger.info(
        "copilot_drop_invalid",
        session_id=state.session_id,
        dropped=list(drop_set),
        remaining=len(filtered),
    )
    rows = [RawImportRow.model_validate(r) for r in filtered]
    return await _validate_rows(
        rows,
        state=state,
        source_sheet_name=state.last_source_sheet,
        limits=limits,
    )


async def _handle_enrich(
    state: CopilotSessionState,
    *,
    limits: TechnicalLimits | None,
) -> CopilotAgentResponse:
    if state.portfolio_id is None:
        raise CopilotFsmError(
            "No portfolio_id on session; validate the blotter first.",
            error_code=WBP_IMPORT_CLIENT,
            http_status=400,
        )
    try:
        result: BuildBaseResponse = await build_base(
            BuildBaseRequest(
                portfolio_id=state.portfolio_id,
                draft_id=state.draft_id,
            ),
            limits=limits,
        )
    except ImportValidationError as exc:
        raise CopilotFsmError(
            str(exc),
            error_code=WBP_IMPORT_BUILD,
            http_status=400,
        ) from exc

    write = build_scratchpad_write(
        result.positions,
        hide_source_sheet=state.last_source_sheet,
    )
    state.current_state = WorkflowState.PORTFOLIO_ENRICHED
    state.portfolio_id = result.portfolio_id
    state.draft_id = result.draft_id
    state.pending_action_ids = []
    sessions.save_session(state)

    warnings = "\n".join(f"- {w}" for w in result.warnings) if result.warnings else ""
    parts = [
        f"Portfolio enriched for **{result.client_alias}** "
        f"({len(result.positions)} position(s)) for portfolioID: `{result.portfolio_id}`. "
        f"Values in `{SCRATCHPAD_SHEET}` tab.",
    ]
    if warnings:
        parts.append(warnings)
    parts.append(
        "Describe a market scenario in the chat "
        "(e.g. “Fed cuts rates 50bp; risk-on equities, 3M horizon”)."
    )
    return _response(
        state,
        content="\n\n".join(parts),
        actions=[],
        badges=["Enter scenario"],
        excel_writes=[write],
        accept_user_text=True,
        placeholder="Describe a market / macro scenario…",
    )
