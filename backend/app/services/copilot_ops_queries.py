"""Read-only / load ops queries for Excel Copilot (across FSM states).

Advisors may ask for PDFs, opti folios, trades, house research, and price
refresh. Identifiers are never created or overwritten on the session from chat
text — only Excel sheets may be refreshed when the user asks to load.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from backend.app.core.supabase import get_service_supabase
from backend.app.services.copilot_ops_format import (
    _format_house_research,
    _format_opti_summary,
    _format_trades,
    _opti_to_enriched,
    _run_to_scenario_response,
)
from backend.app.services.copilot_ops_models import (
    LoadTarget,
    OpsKind,
    OpsQueryMatch,
    OpsQueryResult,
    _ResolvedClient,
    _safe_uuid,
    _unavailable,
)
from backend.app.services.copilot_ops_parse import parse_ops_query

logger = structlog.get_logger(__name__)


def _client():
    return get_service_supabase()

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


__all__ = [
    "LoadTarget",
    "OpsKind",
    "OpsQueryMatch",
    "OpsQueryResult",
    "execute_ops_query",
    "parse_ops_query",
]
