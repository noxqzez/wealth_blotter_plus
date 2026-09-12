"""Deterministic valuation / sizing helpers (scaffold)."""

from __future__ import annotations

from backend.app.schemas.agent_outputs import ProposedOrder, QuantAnalysisOutput
from uuid import UUID


def truncate_ticker(symbol: str, max_len: int = 32) -> str:
    """Ticker normalization — never use an LLM for this."""
    return symbol.strip().upper()[:max_len]


def build_stub_quant_output(
    portfolio_id: UUID,
    symbols: list[str],
) -> QuantAnalysisOutput:
    """Placeholder quant path until real models are plugged in."""
    orders = [
        ProposedOrder(
            symbol=truncate_ticker(sym),
            side="buy",
            quantity=1.0,
        )
        for sym in symbols[:5]
    ]
    return QuantAnalysisOutput(
        portfolio_id=portfolio_id,
        confidence_score=0.5,
        proposed_orders=orders,
        risk_flags=[],
        rationale="Quant stub — replace with deterministic valuation pipeline",
    )
