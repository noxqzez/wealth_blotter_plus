"""Tests for crypto enthusiasm dial (1–5)."""

from __future__ import annotations

from backend.app.services.coin_temperature import (
    DEFAULT_COIN_TEMPERATURE,
    coin_temperature_from_config,
    coin_temperature_scale,
    normalize_coin_temperature,
)


def test_default_is_two() -> None:
    assert DEFAULT_COIN_TEMPERATURE == 2
    assert coin_temperature_from_config({}) == 2
    assert coin_temperature_from_config(None) == 2


def test_normalize_legacy_and_numeric() -> None:
    assert normalize_coin_temperature("cold") == 1
    assert normalize_coin_temperature("neutral") == 3
    assert normalize_coin_temperature("hot") == 5
    assert normalize_coin_temperature(2) == 2
    assert normalize_coin_temperature("4") == 4


def test_scale_relative_to_neutral() -> None:
    assert coin_temperature_scale(3) == 1.0
    assert coin_temperature_scale(2) == 0.75
    assert coin_temperature_scale(1) == 0.5
    assert coin_temperature_scale(2) < coin_temperature_scale(3)
    assert coin_temperature_scale(5) > coin_temperature_scale(3)
