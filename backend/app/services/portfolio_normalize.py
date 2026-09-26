"""Deterministic ticker / Ccy normalization for WF-01 import (no LLM)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from backend.app.schemas.portfolio_import import (
    CurrencyLiteral,
    NormalizedPosition,
    RawImportRow,
)
from backend.app.services.trading_rules_policy import DEFAULT_APPROVED_CRYPTO_BASES

# Fallback only when trading_rules.approved_crypto_universe is unavailable.
CRYPTO_YF_WHITELIST: frozenset[str] = frozenset(DEFAULT_APPROVED_CRYPTO_BASES)
_ASSET_CLASS_MAP: dict[str, str] = {
    "EQUITY": "EQUITY",
    "CASH": "CASH",
    "CRYPTO": "CRYPTO",
    "COINS": "CRYPTO",
    "COIN": "CRYPTO",
}
_MIN_SECURITIES = 5
_MAX_SECURITIES = 10


class ImportValidationError(ValueError):
    """HTTP 400-worthy import boundary / field error."""


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    client_alias: str
    positions: list[NormalizedPosition]
    messages: list[str]


def normalize_import_rows(
    rows: list[RawImportRow],
    *,
    approved_crypto: Sequence[str] | None = None,
) -> NormalizationResult:
    """
    Clean, normalize, aggregate paste rows; inject USD cash if missing.
    Count bound (5–10) is evaluated **after** cash injection.
    User-facing corrections go into ``messages`` (audit / Excel).

    ``approved_crypto`` comes from trading_rules ``approved_crypto_universe``
    (bases like BTC/ETH). Falls back to DEFAULT_APPROVED_CRYPTO_BASES.
    """
    crypto_whitelist = frozenset(
        str(c).strip().upper().removesuffix("-USD")
        for c in (
            approved_crypto
            if approved_crypto is not None
            else DEFAULT_APPROVED_CRYPTO_BASES
        )
        if str(c).strip()
    )
    messages: list[str] = []
    non_empty = [r for r in rows if not _is_empty_row(r)]
    if not non_empty:
        raise ImportValidationError("Input missing required fields")

    aliases = {
        (r.client_alias or "").strip()
        for r in non_empty
        if (r.client_alias or "").strip()
    }
    if len(aliases) > 1:
        raise ImportValidationError("Only 1 client portfolio at a time")
    if not aliases:
        raise ImportValidationError("Input missing client_alias fields")
    client_alias = next(iter(aliases))

    working: list[NormalizedPosition] = []
    for row in non_empty:
        pos, row_msgs = _normalize_one(
            row, client_alias=client_alias, crypto_whitelist=crypto_whitelist
        )
        working.append(pos)
        messages.extend(row_msgs)

    aggregated = _aggregate(working)
    if not any(p.asset_class == "CASH" for p in aggregated):
        aggregated.append(
            NormalizedPosition(
                client_alias=client_alias,
                asset_class="CASH",
                ticker="USD",
                quantity=0.0,
                currency="USD",
                currency_derived=True,
            )
        )
        messages.append("Cash row missing; added CASH USD with quantity 0")

    unique_count = len(aggregated)
    if unique_count < _MIN_SECURITIES or unique_count > _MAX_SECURITIES:
        raise ImportValidationError("Only support between 5 and 10 securities")

    return NormalizationResult(
        client_alias=client_alias,
        positions=aggregated,
        messages=messages,
    )


def _is_empty_row(row: RawImportRow) -> bool:
    return not any(
        [
            (row.client_alias or "").strip(),
            (row.asset_class or "").strip(),
            (row.ticker or "").strip(),
            row.quantity is not None and str(row.quantity).strip() != "",
            (row.currency or "").strip(),
        ]
    )


def _normalize_one(
    row: RawImportRow,
    *,
    client_alias: str,
    crypto_whitelist: frozenset[str],
) -> tuple[NormalizedPosition, list[str]]:
    messages: list[str] = []
    missing: list[str] = []

    raw_alias = (row.client_alias or "").strip()
    if raw_alias and raw_alias != client_alias:
        raise ImportValidationError("Only 1 client portfolio at a time")

    asset_raw = (row.asset_class or "").strip()
    if not asset_raw:
        missing.append("asset_class")
    asset_class = _ASSET_CLASS_MAP.get(asset_raw.upper()) if asset_raw else None
    if asset_raw and asset_class is None:
        raise ImportValidationError(
            f"Unsupported asset_class '{asset_raw}'; use EQUITY, CASH, or CRYPTO"
        )

    raw_ticker = (row.ticker or "").strip()
    if not raw_ticker:
        missing.append("ticker")

    if row.quantity is not None and row.quantity < 0:
        raise ImportValidationError("Negative quantity not allowed")
    raw_qty = row.quantity
    qty = 0.0 if raw_qty is None else float(raw_qty)
    if raw_qty is None:
        messages.append(f"{raw_ticker or 'row'}: blank quantity defaulted to 0")

    if missing:
        raise ImportValidationError(
            f"Input missing {len(missing)} fields: {', '.join(missing)}"
        )

    assert asset_class is not None
    derived_ccy: CurrencyLiteral
    normalized_ticker: str

    if asset_class == "CRYPTO":
        normalized_ticker, derived_ccy = _normalize_crypto(
            raw_ticker, crypto_whitelist=crypto_whitelist
        )
    elif asset_class == "CASH":
        normalized_ticker, derived_ccy = _normalize_cash(raw_ticker)
    else:
        normalized_ticker, derived_ccy = _normalize_equity(raw_ticker)

    if normalized_ticker != raw_ticker.strip().upper():
        messages.append(f"Ticker normalized: {raw_ticker} → {normalized_ticker}")

    advisor_ccy = (row.currency or "").strip().upper() or None
    currency_derived = False
    currency_mismatch = False
    currency: CurrencyLiteral = derived_ccy

    if advisor_ccy is None:
        currency_derived = True
        messages.append(
            f"{normalized_ticker}: Ccy missing; derived {derived_ccy} from ticker"
        )
    elif advisor_ccy not in ("USD", "HKD"):
        raise ImportValidationError(
            f"Unsupported currency '{advisor_ccy}'; use USD or HKD"
        )
    elif advisor_ccy != derived_ccy:
        currency_mismatch = True
        messages.append(
            f"Ccy mismatch for ticker {normalized_ticker} "
            f"(input={advisor_ccy}, ticker={derived_ccy}). "
            f"Aligned to match ticker quoted ccy"
        )
        currency = derived_ccy
    else:
        currency = advisor_ccy  # type: ignore[assignment]

    if not raw_alias:
        messages.append(
            f"{normalized_ticker}: blank client_alias filled with {client_alias}"
        )

    pos = NormalizedPosition(
        client_alias=client_alias,
        asset_class=asset_class,  # type: ignore[arg-type]
        ticker=normalized_ticker,
        quantity=qty,
        currency=currency,
        currency_derived=currency_derived,
        currency_mismatch=currency_mismatch,
    )
    return pos, messages


def _normalize_crypto(
    raw: str,
    *,
    crypto_whitelist: frozenset[str],
) -> tuple[str, CurrencyLiteral]:
    base = raw.strip().upper()
    if base.endswith("-USD"):
        base = base[: -len("-USD")]
    if base not in crypto_whitelist:
        raise ImportValidationError(
            f"Unsupported crypto '{raw}'; trading_rules approved_crypto_universe: "
            f"{', '.join(sorted(crypto_whitelist))}"
        )
    return f"{base}-USD", "USD"


def _normalize_cash(raw: str) -> tuple[str, CurrencyLiteral]:
    ccy = raw.strip().upper()
    if ccy not in ("USD", "HKD"):
        raise ImportValidationError(f"Incorrect asset class for {raw.strip()}")
    return ccy, ccy  # type: ignore[return-value]


def _normalize_equity(raw: str) -> tuple[str, CurrencyLiteral]:
    t = raw.strip().upper()
    if re.fullmatch(r"\d{4}\.HK", t):
        return t, "HKD"
    if re.fullmatch(r"\d{1,4}", t):
        return f"{t.zfill(4)}.HK", "HKD"
    if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,31}", t):
        return t, "USD"
    raise ImportValidationError(
        f"Invalid equity ticker '{raw}'; only HK and US listed stocks currently supported"
    )


def _aggregate(positions: list[NormalizedPosition]) -> list[NormalizedPosition]:
    buckets: dict[str, NormalizedPosition] = {}
    for pos in positions:
        key = pos.ticker
        if key not in buckets:
            buckets[key] = pos.model_copy()
        else:
            prior = buckets[key]
            buckets[key] = prior.model_copy(
                update={"quantity": prior.quantity + pos.quantity}
            )
    return list(buckets.values())
