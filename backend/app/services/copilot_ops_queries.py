"""Read-only / load ops queries for Excel Copilot (across FSM states).

Advisors may ask for PDFs, opti folios, trades, house research, and price
refresh. Identifiers are never created or overwritten on the session from chat
text — only Excel sheets may be refreshed when the user asks to load.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel, Field

from backend.app.core.error_codes import WBP_COPILOT_UNAVAILABLE, new_correlation_id
from backend.app.core.supabase import get_service_supabase
from backend.app.schemas.portfolio_import import EnrichedPosition
from backend.app.schemas.scenario import (
    ClientPortfolioMetrics,
    OptiPosition,
    ScenarioRunResponse,
    TalkingPoint,
)

logger = structlog.get_logger(__name__)

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
    # Persist on session for “load it”
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


def _client():
    return get_service_supabase()


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


_CLIENT_ALIAS_RE = re.compile(
    r"\bclient_alias\s*[:=]?\s*[\"']?([A-Za-z0-9._\-]{1,64})[\"']?",
    re.IGNORECASE,
)
_CLIENT_FOR_RE = re.compile(
    r"\bfor\s+client\s+[\"']?([A-Za-z0-9._\-]{1,64})[\"']?",
    re.IGNORECASE,
)
_CLIENT_BARE_RE = re.compile(
    r"\bclient\s+[\"']?([A-Za-z0-9._\-]{1,64})[\"']?",
    re.IGNORECASE,
)
_PORTFOLIO_ID_RE = re.compile(
    r"\b(?:portfolio(?:_|\s)?id|portfolioid)\s*[:=]?\s*"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{3,})\b",
    re.IGNORECASE,
)
_LOAD_CUES = re.compile(
    r"\b(load(?:\s+it)?|show\s+and\s+load|write\s+to\s+sheet|put\s+on\s+sheet|"
    r"populate|overwrite)\b",
    re.IGNORECASE,
)


def parse_ops_query(text: str) -> OpsQueryMatch | None:
    """Heuristic ops intent. Returns None if not an ops question."""
    lower = (text or "").strip().lower()
    if not lower:
        return None

    want_load = bool(_LOAD_CUES.search(lower))
    client = _extract_client_alias(text)
    portfolio_id, invalid_pid = _extract_portfolio_id(text)

    if re.search(r"\bload\s+it\b", lower) and not any(
        k in lower
        for k in (
            "pdf",
            "portfolio",
            "opti",
            "trade",
            "research",
            "price",
            "enrich",
        )
    ):
        return OpsQueryMatch(kind="load_followup", want_load=True)

    if re.search(r"\b(download|get)\b.*\bpdf\b|\bpdf\b.*\b(download|get)\b", lower):
        return OpsQueryMatch(
            kind="download_pdf",
            client_alias=client,
            portfolio_id=portfolio_id,
            invalid_portfolio_token=invalid_pid,
            want_load=True,
        )

    if re.search(
        r"\b(update|refresh|re-?enrich)\b.*\bprices?\b|"
        r"\bprices?\b.*\b(update|refresh|re-?enrich)\b|"
        r"\bupdate\s+prices\b",
        lower,
    ):
        return OpsQueryMatch(
            kind="update_prices",
            portfolio_id=portfolio_id,
            invalid_portfolio_token=invalid_pid,
            want_load=True,
        )

    if re.search(
        r"\b(optimiz|opti[_\s]?folio|rebalanced)\b.*\bportfolio|"
        r"\boptimized\s+portfolio\b|\bshow\b.*\bopti\b",
        lower,
    ):
        return OpsQueryMatch(
            kind="optimized_portfolio",
            client_alias=client,
            portfolio_id=portfolio_id,
            invalid_portfolio_token=invalid_pid,
            want_load=want_load,
        )

    if re.search(
        r"\b(house|internal)\b.*\bresearch|\bresearch\b.*\b(house|internal|scenario)\b|"
        r"\bwhat\s+did\s+internal\s+research\b",
        lower,
    ):
        return OpsQueryMatch(kind="house_research", want_load=False)

    if re.search(
        r"\b(latest|recent|last)\b.*\btrade|\btrade[s]?\b.*\b(latest|recent|last|for)\b|"
        r"\borders?\b.*\b(client|latest)\b",
        lower,
    ):
        return OpsQueryMatch(
            kind="latest_trades",
            client_alias=client,
            want_load=False,
        )

    if re.search(
        r"\b(latest|current)\b.*\bportfolio\b|\bportfolio\b.*\b(latest|current)\b|"
        r"\bwhat\s+is\s+(the\s+)?latest\s+portfolio\b",
        lower,
    ):
        return OpsQueryMatch(
            kind="latest_portfolio",
            client_alias=client,
            portfolio_id=portfolio_id,
            invalid_portfolio_token=invalid_pid,
            want_load=want_load,
        )

    return None


def _extract_client_alias(text: str) -> str | None:
    for pat in (_CLIENT_ALIAS_RE, _CLIENT_FOR_RE, _CLIENT_BARE_RE):
        m = pat.search(text)
        if m:
            alias = m.group(1).strip()
            if alias.lower() in {
                "the",
                "for",
                "of",
                "pdf",
                "portfolio",
                "trades",
                "alias",
            }:
                continue
            return alias
    return None


def _extract_portfolio_id(text: str) -> tuple[UUID | None, str | None]:
    """Return (uuid, invalid_token). invalid_token set when a non-UUID id was given."""
    m = _PORTFOLIO_ID_RE.search(text)
    if not m:
        return None, None
    raw = m.group(1)
    uid = _safe_uuid(raw)
    if uid is not None:
        return uid, None
    return None, raw


def _find_client_portfolio(
    *,
    client_alias: str | None,
    portfolio_id: UUID | None,
) -> _ResolvedClient | None:
    """
    Resolve client + most recently updated portfolio.

    MVP: no advisor↔client ACL (service-role sees all). Future: scope to advisor.
    """
    try:
        if portfolio_id is not None:
            prow = (
                _client()
                .table("portfolios")
                .select("id, client_id, updated_at")
                .eq("id", str(portfolio_id))
                .limit(1)
                .execute()
            )
            if not prow.data:
                return None
            row = prow.data[0]
            cid = UUID(str(row["client_id"]))
            alias = _client_alias_by_id(cid) or "unknown"
            return _ResolvedClient(
                client_id=cid,
                client_alias=alias,
                portfolio_id=UUID(str(row["id"])),
            )

        if not client_alias:
            return None
        cres = (
            _client()
            .table("clients")
            .select("id, client_alias")
            .ilike("client_alias", client_alias)
            .limit(1)
            .execute()
        )
        if not cres.data:
            return None
        cid = UUID(str(cres.data[0]["id"]))
        alias = str(cres.data[0].get("client_alias") or client_alias)
        pres = (
            _client()
            .table("portfolios")
            .select("id, updated_at")
            .eq("client_id", str(cid))
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        if not pres.data:
            return None
        return _ResolvedClient(
            client_id=cid,
            client_alias=alias,
            portfolio_id=UUID(str(pres.data[0]["id"])),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ops_resolve_client_failed", error=str(exc))
        return None


def _client_alias_by_id(client_id: UUID) -> str | None:
    try:
        res = (
            _client()
            .table("clients")
            .select("client_alias")
            .eq("id", str(client_id))
            .limit(1)
            .execute()
        )
        if res.data:
            return str(res.data[0].get("client_alias") or "") or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("ops_client_alias_failed", error=str(exc))
    return None


def _latest_completed_run(
    *,
    portfolio_id: UUID | None = None,
    scenario_run_id: UUID | None = None,
) -> dict[str, Any] | None:
    try:
        if scenario_run_id is not None:
            res = (
                _client()
                .table("scenario_runs")
                .select("*")
                .eq("id", str(scenario_run_id))
                .eq("status", "COMPLETED")
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        if portfolio_id is None:
            return None
        res = (
            _client()
            .table("scenario_runs")
            .select("*")
            .eq("portfolio_id", str(portfolio_id))
            .eq("status", "COMPLETED")
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("ops_latest_run_failed", error=str(exc))
        return None


def _format_opti_summary(
    *,
    client_alias: str,
    portfolio_id: UUID,
    run: dict[str, Any],
) -> str:
    opti = run.get("opti_folio") or []
    if not isinstance(opti, list):
        opti = []
    lines = [
        f"**Latest optimized portfolio** for client `{client_alias}` "
        f"(portfolioID `{portfolio_id}`, scenarioID `{run.get('id')}`):\n"
    ]
    if not opti:
        lines.append("_No positions in opti_folio._")
        return "\n".join(lines)
    lines.append("| Ticker | Class | Qty | Weight% | Notional |")
    lines.append("|---|---|---:|---:|---:|")
    for raw in opti[:40]:
        if not isinstance(raw, dict):
            continue
        w = raw.get("weight_pct")
        w_pct = f"{float(w) * 100:.2f}" if w is not None else "—"
        lines.append(
            f"| `{raw.get('ticker')}` | {raw.get('asset_class')} | "
            f"{raw.get('quantity')} | {w_pct} | {raw.get('notional_value')} |"
        )
    if len(opti) > 40:
        lines.append(f"_…and {len(opti) - 40} more._")
    return "\n".join(lines)


def _opti_to_enriched(
    *,
    client_alias: str,
    portfolio_id: UUID,
    opti: list[Any],
) -> list[EnrichedPosition]:
    from datetime import datetime, timezone

    out: list[EnrichedPosition] = []
    for raw in opti:
        if not isinstance(raw, dict):
            continue
        try:
            qty = float(raw.get("quantity") or 0)
            spot = float(raw.get("spot_price") or 0)
            notional = raw.get("notional_value")
            mv = float(notional) if notional is not None else qty * spot
            w = float(raw.get("weight_pct") or 0)
            ccy = str(raw.get("currency") or "USD").upper()
            if ccy not in {"USD", "HKD"}:
                ccy = "USD"
            ac = str(raw.get("asset_class") or "EQUITY").upper()
            if ac not in {"EQUITY", "CASH", "CRYPTO"}:
                ac = "EQUITY"
            out.append(
                EnrichedPosition(
                    pos_id=uuid4(),
                    portfolio_id=portfolio_id,
                    client_alias=client_alias,
                    asset_class=ac,  # type: ignore[arg-type]
                    ticker=str(raw.get("ticker") or "").upper(),
                    quantity=qty,
                    spot_price=spot,
                    market_value=mv,
                    currency=ccy,  # type: ignore[arg-type]
                    short_name=str(raw.get("ticker") or ""),
                    current_weight_pct=w,
                    price_timestamp=datetime.now(timezone.utc),
                    status="VALID",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ops_opti_row_skip", error=str(exc))
    return out


def _run_to_scenario_response(run: dict[str, Any]) -> ScenarioRunResponse:
    from backend.app.schemas.scenario import ScenarioBlock, ScenarioParams

    opti_raw = run.get("opti_folio") or []
    positions: list[OptiPosition] = []
    if isinstance(opti_raw, list):
        for raw in opti_raw:
            if isinstance(raw, dict):
                try:
                    positions.append(OptiPosition.model_validate(raw))
                except Exception:  # noqa: BLE001
                    continue
    talking_raw = run.get("talking_points") or []
    points: list[TalkingPoint] = []
    if isinstance(talking_raw, list):
        for item in talking_raw:
            if isinstance(item, dict) and item.get("bullet"):
                points.append(TalkingPoint(bullet=str(item["bullet"])))
            elif isinstance(item, str):
                points.append(TalkingPoint(bullet=item))

    base_m = run.get("risk_metrics_base")
    opti_m = run.get("risk_metrics_opti")
    params_raw = run.get("scenario_params")
    try:
        params = (
            ScenarioParams.model_validate(params_raw)
            if isinstance(params_raw, dict)
            else ScenarioParams(
                user_prompt=str(run.get("user_prompt") or "ops_query"),
                scenario=ScenarioBlock(
                    scenario_name="Loaded scenario",
                    scenario_type="custom_scenario",
                ),
            )
        )
    except Exception:  # noqa: BLE001
        params = ScenarioParams(
            user_prompt=str(run.get("user_prompt") or "ops_query"),
            scenario=ScenarioBlock(
                scenario_name="Loaded scenario",
                scenario_type="custom_scenario",
            ),
        )
    phase = str(run.get("phase") or "phase1")
    if phase not in {"phase1", "phase2"}:
        phase = "phase1"
    return ScenarioRunResponse(
        scenario_run_id=UUID(str(run["id"])),
        portfolio_id=UUID(str(run["portfolio_id"])),
        status="COMPLETED",
        phase=phase,  # type: ignore[arg-type]
        scenario_params=params,
        opti_folio=positions,
        talking_points=points,
        base_client_metrics=(
            ClientPortfolioMetrics.model_validate(base_m)
            if isinstance(base_m, dict)
            else None
        ),
        opti_client_metrics=(
            ClientPortfolioMetrics.model_validate(opti_m)
            if isinstance(opti_m, dict)
            else None
        ),
    )


def _format_house_research(run: dict[str, Any]) -> str:
    sv = run.get("strategy_view")
    if not isinstance(sv, dict) or not sv:
        return ""
    summary = str(sv.get("summary") or "").strip()
    notes = sv.get("notes") or []
    views = sv.get("asset_views") or []
    lines = [
        f"**Internal / house research** (scenarioID `{run.get('id')}`, "
        f"source `{sv.get('source') or '—'}`):\n"
    ]
    if summary:
        lines.append(summary)
    if isinstance(notes, list) and notes:
        lines.append("\nNotes:")
        for n in notes[:8]:
            lines.append(f"- {n}")
    if isinstance(views, list) and views:
        lines.append("\n| Asset | View | Conviction |")
        lines.append("|---|---|---:|")
        for v in views[:30]:
            if not isinstance(v, dict):
                continue
            lines.append(
                f"| `{v.get('asset')}` | {v.get('final_view')} | "
                f"{v.get('final_conviction')} |"
            )
    return "\n".join(lines)


def _format_trades(rows: list[dict[str, Any]], *, client_alias: str) -> str:
    lines = [f"**Latest trades** for client `{client_alias}`:\n"]
    if not rows:
        return ""
    lines.append("| Order | Status | Ticker | Side | Qty |")
    lines.append("|---|---|---|---|---:|")
    for r in rows[:30]:
        lines.append(
            f"| `{r.get('order_id')}` | {r.get('status')} | `{r.get('ticker')}` | "
            f"{r.get('side')} | {r.get('quantity')} |"
        )
    return "\n".join(lines)


async def execute_ops_query(
    match: OpsQueryMatch,
    *,
    advisor_id: UUID,
    session_portfolio_id: UUID | None,
    session_scenario_run_id: UUID | None,
    session_ops_kind: str | None = None,
    session_ops_portfolio_id: UUID | None = None,
    session_ops_scenario_run_id: UUID | None = None,
    session_ops_client_alias: str | None = None,
    session_ops_load_target: str | None = None,
    limits: Any = None,
) -> OpsQueryResult:
    """Execute a matched ops query (read / download / optional Excel load)."""
    # advisor_id reserved for future client↔advisor ACL (MVP: open via service role).

    if match.invalid_portfolio_token:
        return _unavailable(
            "Request unavailable — portfolioID must be a UUID "
            f"(got `{match.invalid_portfolio_token}`)."
        )

    if match.kind == "load_followup":
        if not session_ops_kind or not session_ops_load_target:
            return _unavailable("Nothing to load — ask to show a portfolio or trades first.")
        follow = OpsQueryMatch(
            kind=session_ops_kind,  # type: ignore[arg-type]
            client_alias=session_ops_client_alias,
            portfolio_id=session_ops_portfolio_id,
            scenario_run_id=session_ops_scenario_run_id,
            want_load=True,
        )
        return await execute_ops_query(
            follow,
            advisor_id=advisor_id,
            session_portfolio_id=session_portfolio_id,
            session_scenario_run_id=session_scenario_run_id,
            limits=limits,
        )

    if match.kind == "update_prices":
        pid = match.portfolio_id or session_portfolio_id
        if pid is None:
            return _unavailable(
                "Request unavailable — no portfolio in session scratchpad context. "
                "Enrich a portfolio first, or pass a portfolioID."
            )
        try:
            from backend.app.schemas.portfolio_import import BuildBaseRequest
            from backend.app.services.portfolio_import import build_base

            result = await build_base(
                BuildBaseRequest(portfolio_id=pid, draft_id=None),
                limits=limits,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ops_update_prices_failed", error=str(exc))
            return _unavailable(
                f"Request unavailable — could not refresh prices ({exc})."
            )
        return OpsQueryResult(
            ok=True,
            content=(
                f"Updated prices for portfolioID `{pid}` "
                f"({len(result.positions)} position(s)). "
                f"`portfolio_scratchpad` will refresh."
            ),
            excel_scratchpad_positions=list(result.positions),
            ops_kind="update_prices",
            ops_portfolio_id=pid,
            ops_client_alias=result.client_alias,
            ops_load_target="scratchpad",
        )

    if match.kind == "house_research":
        run = None
        if session_scenario_run_id is not None:
            run = _latest_completed_run(scenario_run_id=session_scenario_run_id)
            if run is None:
                # Allow non-completed current run for research if present
                try:
                    res = (
                        _client()
                        .table("scenario_runs")
                        .select("*")
                        .eq("id", str(session_scenario_run_id))
                        .limit(1)
                        .execute()
                    )
                    run = res.data[0] if res.data else None
                except Exception:  # noqa: BLE001
                    run = None
        if run is None and session_portfolio_id is not None:
            run = _latest_completed_run(portfolio_id=session_portfolio_id)
        if run is None:
            return _unavailable(
                "Request unavailable — no scenario research found for this session."
            )
        body = _format_house_research(run)
        if not body:
            return _unavailable(
                "Request unavailable — no house/internal research on this scenario."
            )
        return OpsQueryResult(
            ok=True,
            content=body,
            ops_kind="house_research",
            ops_scenario_run_id=UUID(str(run["id"])),
            ops_portfolio_id=UUID(str(run["portfolio_id"])),
            ops_load_target="none",
        )

    resolved = _find_client_portfolio(
        client_alias=match.client_alias,
        portfolio_id=match.portfolio_id,
    )
    if match.kind in {
        "download_pdf",
        "latest_portfolio",
        "optimized_portfolio",
        "latest_trades",
    } and resolved is None:
        # Fall back to session portfolio when alias omitted
        if match.client_alias is None and match.portfolio_id is None and session_portfolio_id:
            resolved = _find_client_portfolio(
                client_alias=None,
                portfolio_id=session_portfolio_id,
            )
        if resolved is None:
            return _unavailable(
                "Request unavailable — client/portfolio not found."
            )

    assert resolved is not None or match.kind == "latest_trades"

    if match.kind == "latest_trades":
        alias = (match.client_alias or (resolved.client_alias if resolved else "") or "").strip()
        if not alias:
            return _unavailable("Request unavailable — specify a client_alias.")
        try:
            tres = (
                _client()
                .table("staged_trades")
                .select(
                    "order_id,status,ticker,asset_class,side,quantity,created_at,"
                    "scenario_run_id,client_alias"
                )
                .ilike("client_alias", alias)
                .order("created_at", desc=True)
                .limit(30)
                .execute()
            )
            rows = list(tres.data or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("ops_trades_failed", error=str(exc))
            return _unavailable(f"Request unavailable — could not load trades ({exc}).")
        if not rows:
            return _unavailable(
                f"Request unavailable — no trades found for client `{alias}`."
            )
        return OpsQueryResult(
            ok=True,
            content=_format_trades(rows, client_alias=alias),
            ops_kind="latest_trades",
            ops_client_alias=alias,
            ops_load_target="none",
        )

    assert resolved is not None
    run = _latest_completed_run(portfolio_id=resolved.portfolio_id)
    if match.kind == "download_pdf":
        if run is None:
            return _unavailable(
                f"Request unavailable — no COMPLETED scenario PDF for client "
                f"`{resolved.client_alias}`."
            )
        try:
            import base64

            from backend.app.services.scenario_run import build_pdf_bytes

            # build_pdf_bytes filters by advisor_id — use run's advisor for MVP open ACL
            run_advisor = UUID(str(run["advisor_id"]))
            pdf_bytes, filename, _alias = await build_pdf_bytes(
                scenario_run_id=UUID(str(run["id"])),
                advisor_id=run_advisor,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ops_pdf_failed", error=str(exc))
            return _unavailable(f"Request unavailable — PDF could not be built ({exc}).")
        return OpsQueryResult(
            ok=True,
            content=(
                f"PDF ready for client `{resolved.client_alias}` "
                f"(scenarioID `{run['id']}`): **{filename}**."
            ),
            download_base64=base64.b64encode(pdf_bytes).decode("ascii"),
            download_filename=filename,
            ops_kind="download_pdf",
            ops_portfolio_id=resolved.portfolio_id,
            ops_scenario_run_id=UUID(str(run["id"])),
            ops_client_alias=resolved.client_alias,
            ops_load_target="pdf",
        )

    if match.kind in {"latest_portfolio", "optimized_portfolio"}:
        if run is None:
            return _unavailable(
                f"Request unavailable — no COMPLETED optimized portfolio for "
                f"`{resolved.client_alias}` / portfolioID `{resolved.portfolio_id}`."
            )
        summary = _format_opti_summary(
            client_alias=resolved.client_alias,
            portfolio_id=resolved.portfolio_id,
            run=run,
        )
        result = OpsQueryResult(
            ok=True,
            content=summary,
            ops_kind=match.kind,
            ops_portfolio_id=resolved.portfolio_id,
            ops_scenario_run_id=UUID(str(run["id"])),
            ops_client_alias=resolved.client_alias,
            ops_load_target=(
                "scratchpad" if match.kind == "latest_portfolio" else "rebalanced"
            ),
        )
        if match.want_load:
            if match.kind == "latest_portfolio":
                result.excel_scratchpad_positions = _opti_to_enriched(
                    client_alias=resolved.client_alias,
                    portfolio_id=resolved.portfolio_id,
                    opti=list(run.get("opti_folio") or []),
                )
                result.content += (
                    f"\n\nLoaded into `{SCRATCHPAD}` (overwrites prior sheet)."
                )
            else:
                try:
                    result.excel_rebalanced = _run_to_scenario_response(run)
                    result.content += (
                        f"\n\nLoaded into `{REBALANCED}` (overwrites prior sheet)."
                    )
                except Exception as exc:  # noqa: BLE001
                    return _unavailable(
                        f"Request unavailable — could not build rebalanced sheet ({exc})."
                    )
        else:
            target = (
                "`portfolio_scratchpad`"
                if match.kind == "latest_portfolio"
                else "`rebalanced_portfolio`"
            )
            result.content += f"\n\nSay **load it** to write this into {target}."
        return result

    return _unavailable("Request unavailable — unrecognized ops query.")


SCRATCHPAD = "portfolio_scratchpad"
REBALANCED = "rebalanced_portfolio"

__all__ = [
    "OpsQueryMatch",
    "OpsQueryResult",
    "execute_ops_query",
    "parse_ops_query",
]
