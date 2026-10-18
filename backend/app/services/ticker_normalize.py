"""Shared ticker normalization for scenario / research / staging paths."""

from __future__ import annotations

_APPROVED_CRYPTO_BASES = frozenset({"BTC", "ETH", "DOGE", "XRP", "SOL"})
_CASH_LIKE = frozenset({"CASH", "CASH_USD", "USD", "HKD"})


def normalize_ticker(raw: str) -> str:
    """
    Canonical ticker form used by scenario_run / research / staged trades.

    - Cash aliases: ``CASH_USD`` → ``USD``; ``CASH`` / ``USD`` / ``HKD`` unchanged
    - Approved crypto bases → ``BASE-USD`` (Yahoo-style)
    """
    t = str(raw or "").strip().upper()
    if not t:
        return t
    if t in _CASH_LIKE:
        return "USD" if t == "CASH_USD" else t
    base = t.removesuffix("-USD").removesuffix("USD")
    if base in _APPROVED_CRYPTO_BASES and not t.endswith("-USD"):
        return f"{base}-USD"
    if t.endswith("-USD") and base in _APPROVED_CRYPTO_BASES:
        return f"{base}-USD"
    return t
