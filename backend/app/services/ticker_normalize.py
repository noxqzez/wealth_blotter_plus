"""Shared ticker normalization for scenario / research / staging / tilts."""

from __future__ import annotations

import re

_APPROVED_CRYPTO_BASES = frozenset({"BTC", "ETH", "DOGE", "XRP", "SOL"})
_CASH_LIKE = frozenset({"CASH", "CASH_USD", "USD", "HKD"})

# Yahoo / import canonical form is zero-padded 4-digit + ``.HK`` (e.g. 0700.HK).
_HK_DOT = re.compile(r"^0*(\d{1,5})\.HK$", re.IGNORECASE)
_HK_SPACE = re.compile(r"^0*(\d{1,5})\s+HK$", re.IGNORECASE)
_HK_BARE = re.compile(r"^0*(\d{1,5})$")


def normalize_hk_code(raw: str) -> str | None:
    """
    Return Yahoo-style ``NNNN.HK`` if ``raw`` looks like an HK equity code.

    Accepts ``700``, ``0700``, ``700.HK``, ``0700.HK``, ``700 HK``.
    """
    t = str(raw or "").strip().upper()
    if not t:
        return None
    m = _HK_DOT.fullmatch(t) or _HK_SPACE.fullmatch(t) or _HK_BARE.fullmatch(t)
    if not m:
        return None
    num = int(m.group(1))
    if num <= 0 or num > 99999:
        return None
    return f"{num:04d}.HK"


def normalize_ticker(raw: str) -> str:
    """
    Canonical ticker form used by scenario / research / staged trades / tilts.

    - Cash aliases: ``CASH_USD`` → ``USD``; ``CASH`` / ``USD`` / ``HKD`` unchanged
    - Approved crypto bases → ``BASE-USD`` (Yahoo-style)
    - HK equities → zero-padded ``NNNN.HK`` (``700`` / ``700.HK`` → ``0700.HK``)
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
    hk = normalize_hk_code(t)
    if hk is not None:
        return hk
    return t
