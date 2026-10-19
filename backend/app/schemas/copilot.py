"""Pydantic contracts for the Excel Copilot FSM (main workflow chat).

MVP: linear WF-01 → WF-02 state machine with opaque action IDs, Excel write
intents (client applies only after action responses), and chat-side scenario
params (no scenario_params sheet). Recursive re-entry is deferred.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# FSM
# ---------------------------------------------------------------------------


class WorkflowState(str, Enum):
    INIT = "INIT"
    BLOTTER_HAS_ERRORS = "BLOTTER_HAS_ERRORS"
    BLOTTER_VALIDATED = "BLOTTER_VALIDATED"
    PORTFOLIO_ENRICHED = "PORTFOLIO_ENRICHED"
    SCENARIO_PARSED = "SCENARIO_PARSED"
    OPTIMIZATION_COMPLETE = "OPTIMIZATION_COMPLETE"
    ORDERS_PLACED = "ORDERS_PLACED"


# User-facing status chip labels (Excel task pane).
WORKFLOW_STATUS_LABELS: dict[WorkflowState, str] = {
    WorkflowState.INIT: "Import securities",
    WorkflowState.BLOTTER_HAS_ERRORS: "Fix import errors",
    WorkflowState.BLOTTER_VALIDATED: "Blotter validated",
    WorkflowState.PORTFOLIO_ENRICHED: "Enter scenario",
    WorkflowState.SCENARIO_PARSED: "Review scenario",
    WorkflowState.OPTIMIZATION_COMPLETE: "Optimization complete",
    WorkflowState.ORDERS_PLACED: "Orders placed",
}


def workflow_status_label(state: WorkflowState) -> str:
    return WORKFLOW_STATUS_LABELS.get(state, state.value)


class ActionVariant(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    DANGER = "danger"  # reserved; MVP never bypasses trading_rules


class IntentClass(str, Enum):
    WORKFLOW_COMMAND = "WORKFLOW_COMMAND"
    FINANCIAL_INQUIRY = "FINANCIAL_INQUIRY"
    OFF_TOPIC_NONSENSE = "OFF_TOPIC_NONSENSE"
    PROMPT_INJECTION = "PROMPT_INJECTION"
    SCENARIO_PROMPT = "SCENARIO_PROMPT"
    PARAM_EDIT = "PARAM_EDIT"
    # Domain IDs: chat may recall/audit (read-only) but never set/write them.
    ID_RECALL = "ID_RECALL"
    ID_WRITE_ATTEMPT = "ID_WRITE_ATTEMPT"
    OPS_QUERY = "OPS_QUERY"


# ---------------------------------------------------------------------------
# Actions / guardrails / Excel intents
# ---------------------------------------------------------------------------


class ActionTrigger(BaseModel):
    """Opaque action shown as a chat button; client POSTs ``id`` to the handler."""

    id: str = Field(..., min_length=1, max_length=64)
    label: str = Field(..., min_length=1, max_length=128)
    variant: ActionVariant = ActionVariant.PRIMARY
    # Server-owned; client must not invent endpoints — kept for UI hints only.
    requires_excel_selection: bool = False
    requires_user_text: bool = False


class GuardrailCheck(BaseModel):
    rule_id: str = Field(..., min_length=1, max_length=128)
    severity: Literal["info", "warning", "blocking_error"]
    message: str
    suggested_fix: str | None = None


class ExcelValueFontStyle(BaseModel):
    """Per-cell font style when a data cell equals a target value."""

    column: str = Field(..., min_length=1, description="Header name of the column.")
    equals: str = Field(
        ...,
        min_length=1,
        description="Case-insensitive match against the cell text.",
    )
    bold: bool = False
    font_color: str | None = Field(
        default=None,
        description="Excel hex without # (e.g. C00000 for red).",
    )


class ExcelWriteSpec(BaseModel):
    """Client applies these ONLY when returned from an action response."""

    sheet_name: str = Field(..., min_length=1, max_length=64)
    values: list[list[Any]] = Field(
        default_factory=list,
        description="2D grid including header row when present.",
    )
    activate: bool = True
    hide_source_sheet: str | None = Field(
        default=None,
        description="Optional paste-sheet name to clear+hide after write.",
    )
    column_number_formats: dict[str, str] = Field(
        default_factory=dict,
        description="Header name → Excel number format for data rows.",
    )
    header_fill_colors: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Header name → fill color (Excel hex without #, e.g. D9D9D9). "
            "Used to distinguish advisor-input vs system-enriched columns."
        ),
    )
    header_row_indexes: list[int] = Field(
        default_factory=list,
        description=(
            "0-based row indexes that are table headers (fills/bold/align + "
            "number formats for rows below until the next header or blank). "
            "Empty ⇒ treat row 0 as the sole header when styling is requested."
        ),
    )
    header_bold: bool = Field(
        default=False,
        description="Bold the header row when true.",
    )
    header_horizontal_alignment: Literal["left", "center", "right"] | None = Field(
        default=None,
        description="Optional header text alignment.",
    )
    table_border: bool = Field(
        default=False,
        description="Apply a continuous border around/inside the written grid.",
    )
    value_font_styles: list[ExcelValueFontStyle] = Field(
        default_factory=list,
        description=(
            "Optional data-cell font rules (e.g. bold green BUY / bold red SELL)."
        ),
    )


class DownloadArtifact(BaseModel):
    kind: Literal["pdf"] = "pdf"
    filename: str
    content_base64: str


class ChatMessageView(BaseModel):
    message_id: str
    role: Literal["assistant", "user", "system"]
    content: str
    created_at: str | None = None


# ---------------------------------------------------------------------------
# Layer-1 input validation (Copilot chat only; scenario parse keeps own limits)
# ---------------------------------------------------------------------------


class CopilotUserText(BaseModel):
    """Structural pre-validation for free-text chat (max 1000 chars)."""

    text: str = Field(..., min_length=1, max_length=1000)

    @field_validator("text")
    @classmethod
    def reject_key_mash(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Message cannot be empty.")
        letters = "".join(ch for ch in stripped.lower() if ch.isalpha())
        if letters and len(letters) <= 12 and not any(v in letters for v in "aeiou"):
            raise ValueError("Message looks like keyboard mash; please rephrase.")
        return stripped


# ---------------------------------------------------------------------------
# API I/O
# ---------------------------------------------------------------------------


class CopilotAgentResponse(BaseModel):
    message_id: str
    session_id: str
    current_state: WorkflowState
    # Friendly chip for the Excel pane (e.g. INIT → "Import securities").
    status_label: str = ""
    content: str
    guardrails: list[GuardrailCheck] = Field(default_factory=list)
    actions: list[ActionTrigger] = Field(default_factory=list)
    execution_status_badges: list[str] = Field(default_factory=list)
    excel_writes: list[ExcelWriteSpec] = Field(default_factory=list)
    download: DownloadArtifact | None = None
    accept_user_text: bool = False
    user_text_placeholder: str | None = None
    # When true, Excel renders a compact ★★★★★ control (posts rate_stars_N).
    show_star_rating: bool = False
    # Optional structured context for the pane (not Excel-written).
    scenario_params_summary: dict[str, Any] | None = None
    portfolio_id: UUID | None = None
    scenario_run_id: UUID | None = None


class CopilotSessionCreateResponse(CopilotAgentResponse):
    """Alias shape for POST /sessions."""


class CopilotActionRequest(BaseModel):
    action_id: str = Field(..., min_length=1, max_length=64)
    # Client-supplied context for selection-backed actions.
    rows: list[dict[str, Any]] | None = None
    source_sheet_name: str | None = Field(default=None, max_length=64)
    # Current ord_blotter rows for Place All Orders (HITL compare vs staged).
    order_rows: list[dict[str, Any]] | None = None


class CopilotMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)


class CopilotSessionState(BaseModel):
    """Server-side session snapshot (in-memory MVP; not a public API model)."""

    session_id: str
    advisor_id: UUID
    current_state: WorkflowState = WorkflowState.INIT
    portfolio_id: UUID | None = None
    draft_id: UUID | None = None
    scenario_run_id: UUID | None = None
    scenario_params: dict[str, Any] | None = None
    params_dirty: bool = False
    last_import_rows: list[dict[str, Any]] | None = None
    last_source_sheet: str | None = None
    last_validation_error: str | None = None
    pending_action_ids: list[str] = Field(default_factory=list)
    # Set when advisor uses Reprocess / Drop Invalid before a clean validate.
    import_hitl: bool = False
    # Optional post-opti star rating: one per scenario_run (dismissed or saved).
    rating_captured: bool = False
    rating_for_run_id: UUID | None = None
    # Last ops-query result so “load it” can apply Excel without re-asking.
    last_ops_kind: str | None = None
    last_ops_portfolio_id: UUID | None = None
    last_ops_scenario_run_id: UUID | None = None
    last_ops_client_alias: str | None = None
    last_ops_load_target: str | None = None  # scratchpad | rebalanced | pdf

