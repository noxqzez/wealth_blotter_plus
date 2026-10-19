"""Format helpers for Copilot ops query responses."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import structlog

from backend.app.schemas.portfolio_import import EnrichedPosition
from backend.app.schemas.scenario import (
    ClientPortfolioMetrics,
    OptiPosition,
    ScenarioRunResponse,
    TalkingPoint,
)

logger = structlog.get_logger(__name__)

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
