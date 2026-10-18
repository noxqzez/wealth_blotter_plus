"""Backward-compatible re-exports — prefer ``backend.app.services.market_data``."""

from __future__ import annotations

from backend.app.services.market_data import fetch_crypto_history, fetch_news

__all__ = ["fetch_crypto_history", "fetch_news"]
