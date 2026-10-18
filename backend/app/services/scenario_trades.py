"""Pure helpers for opti→staged-trade construction (no DB I/O)."""

from __future__ import annotations

from typing import Any, Mapping, Sequence
from uuid import UUID

from backend.app.schemas.scenario import StagedTrade
from backend.app.services.ticker_normalize import normalize_ticker as _normalize_ticker

_QTY_EPS = 1e-6
_WEIGHT_EPS = 1e-8

def _position_maps(
    positions: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, float], dict[str, float], dict[str, str], float]:
    """Return weights, quantities, asset_classes, portfolio_value."""
    weights: dict[str, float] = {}
    quantities: dict[str, float] = {}
    asset_classes: dict[str, str] = {}
    total = 0.0
    for pos in positions:
        ticker = _normalize_ticker(str(pos.get("ticker", "")))
        if not ticker:
            continue
        ac = str(pos.get("asset_class", "EQUITY")).strip().upper() or "EQUITY"
        if ac == "CRYPTO":
            ticker = _normalize_ticker(ticker)
        qty = float(pos.get("quantity") or 0.0)
        notional = pos.get("notional_value")
        w = pos.get("current_weight_pct")
        quantities[ticker] = qty
        asset_classes[ticker] = ac
        if notional is not None:
            total += float(notional)
        if w is not None:
            weights[ticker] = float(w)
    if not weights and total > 0:
        for pos in positions:
            ticker = _normalize_ticker(str(pos.get("ticker", "")))
            notional = pos.get("notional_value")
            if ticker and notional is not None:
                weights[ticker] = float(notional) / total
    return weights, quantities, asset_classes, total


def _latest_prices_from_bars(bars: Sequence[Any]) -> dict[str, float]:
    by_ticker: dict[str, tuple[str, float]] = {}
    for bar in bars:
        ticker = _normalize_ticker(str(getattr(bar, "ticker", "")))
        price_date = str(getattr(bar, "price_date", ""))
        close = float(getattr(bar, "close_price", 0.0) or 0.0)
        if not ticker or close <= 0:
            continue
        prev = by_ticker.get(ticker)
        if prev is None or price_date >= prev[0]:
            by_ticker[ticker] = (price_date, close)
    return {t: px for t, (_d, px) in by_ticker.items()}


def _build_staged_trades(
    *,
    scenario_run_id: UUID,
    base_qty: Mapping[str, float],
    base_weights: Mapping[str, float],
    opti_positions: Sequence[Mapping[str, Any]],
    asset_classes: Mapping[str, str],
    client_alias: str | None = None,
) -> list[StagedTrade]:
    opti_by_ticker = {
        _normalize_ticker(str(p.get("ticker", ""))): p for p in opti_positions if p.get("ticker")
    }
    tickers = sorted(set(base_qty) | set(opti_by_ticker) | set(base_weights))
    trades: list[StagedTrade] = []
    seq = 0
    short = scenario_run_id.hex[:8].upper()
    alias = (client_alias or "").strip() or None
    for ticker in tickers:
        if ticker in {"CASH", "CASH_USD"}:
            continue
        bq = float(base_qty.get(ticker, 0.0))
        opti = opti_by_ticker.get(ticker, {})
        oq = float(opti.get("quantity") or 0.0)
        bw = float(base_weights.get(ticker, 0.0))
        ow = float(opti.get("weight_pct") or 0.0)
        dq = oq - bq
        if abs(dq) < _QTY_EPS and abs(ow - bw) < _WEIGHT_EPS:
            continue
        # Prefer quantity delta; fall back to weight-driven side
        if abs(dq) >= _QTY_EPS:
            side = "BUY" if dq > 0 else "SELL"
            qty = abs(dq)
        else:
            side = "BUY" if ow > bw else "SELL"
            qty = abs(ow - bw)
        ac_raw = str(
            opti.get("asset_class")
            or asset_classes.get(ticker)
            or ("CRYPTO" if ticker.endswith("-USD") else "EQUITY")
        ).upper()
        if ac_raw not in {"EQUITY", "CASH", "CRYPTO"}:
            ac_raw = "EQUITY"
        seq += 1
        notional = opti.get("notional_value")
        if notional is not None and bq > 0 and abs(dq) >= _QTY_EPS:
            # Approximate trade notional from opti unit value when possible
            unit = float(notional) / oq if oq else None
            notional_trade = float(unit * qty) if unit else None
        else:
            notional_trade = float(notional) if notional is not None and bq == 0 else None
        # Equity whole shares; crypto 2 dp (match opti_folio).
        if ac_raw == "CASH":
            qty_out = round(float(qty), 2)
        elif ac_raw == "CRYPTO":
            qty_out = round(float(qty), 2)
        else:
            qty_out = float(int(qty))
        if qty_out <= 0 and ac_raw != "CASH":
            continue
        trades.append(
            StagedTrade(
                order_id=f"ST-{short}-{ticker}-{side}-{seq:03d}",
                client_alias=alias,
                status="Pending",
                ticker=ticker,
                asset_class=ac_raw,  # type: ignore[arg-type]
                side=side,  # type: ignore[arg-type]
                quantity=qty_out,
                exec_type="Market",
                notional_value=(
                    round(float(notional_trade), 2)
                    if notional_trade is not None
                    else None
                ),
                from_weight_pct=round(float(bw), 6),
                to_weight_pct=round(float(ow), 6),
            )
        )
    return trades


