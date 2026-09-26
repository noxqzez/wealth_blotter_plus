"""Management crypto-enthusiasm dial (1–5).

``3`` = neutral (tilt scale 1.0). Default ``2`` until a management dashboard exists.
Legacy labels ``cold``/``neutral``/``hot`` map to 1 / 3 / 5.
"""

from __future__ import annotations

from typing import Any, Mapping

# Tilt multiplier applied to CRYPTO asset-class views in BL / stub strategist.
COIN_TEMPERATURE_SCALE: dict[int, float] = {
    1: 0.50,
    2: 0.75,
    3: 1.00,
    4: 1.125,
    5: 1.25,
}

DEFAULT_COIN_TEMPERATURE: int = 2

_LEGACY_LABELS: dict[str, int] = {
    "cold": 1,
    "cool": 2,
    "neutral": 3,
    "warm": 4,
    "hot": 5,
}


def normalize_coin_temperature(raw: Any, *, default: int = DEFAULT_COIN_TEMPERATURE) -> int:
    """Coerce config / strategy_view value to dial level 1–5."""
    if raw is None:
        return default
    if isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        level = int(raw)
        if 1 <= level <= 5:
            return level
        return default
    text = str(raw).strip().lower()
    if not text:
        return default
    if text in _LEGACY_LABELS:
        return _LEGACY_LABELS[text]
    try:
        level = int(float(text))
    except ValueError:
        return default
    if 1 <= level <= 5:
        return level
    return default


def coin_temperature_from_config(config: Mapping[str, Any] | None) -> int:
    """Read dial from business_guardrails-like mapping."""
    if not config:
        return DEFAULT_COIN_TEMPERATURE
    return normalize_coin_temperature(
        config.get("coin_temperature"), default=DEFAULT_COIN_TEMPERATURE
    )


def coin_temperature_scale(level: Any) -> float:
    """CRYPTO view tilt multiplier for a dial level (or legacy label)."""
    return COIN_TEMPERATURE_SCALE[normalize_coin_temperature(level)]
