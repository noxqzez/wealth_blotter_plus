"""Shared outbound market-data throttle: RPM, burst, spacing, cache, agent budget."""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator, TypeVar

import structlog

from backend.app.schemas.rules import MarketDataLimits, TechnicalLimits

logger = structlog.get_logger(__name__)

T = TypeVar("T")


class MarketDataBudgetExceeded(Exception):
    """Raised when an agent run exceeds its soft outbound call budget."""


class MarketDataThrottleError(Exception):
    """Raised when throttle waits exceed a hard safety bound."""


@dataclass(frozen=True, slots=True)
class ProviderThrottleProfile:
    name: str
    outbound_rpm: int
    burst_calls: int
    burst_window_seconds: int
    max_concurrency: int
    min_spacing_ms: int
    cache_ttl_seconds: int
    max_calls_per_agent_run: int
    max_retries: int


def yahoo_profile(md: MarketDataLimits) -> ProviderThrottleProfile:
    return ProviderThrottleProfile(
        name="yahoo",
        outbound_rpm=md.yahoo_outbound_rpm,
        burst_calls=md.yahoo_burst_calls,
        burst_window_seconds=md.yahoo_burst_window_seconds,
        max_concurrency=md.yahoo_max_concurrency,
        min_spacing_ms=md.yahoo_min_spacing_ms,
        cache_ttl_seconds=md.yahoo_cache_ttl_seconds,
        max_calls_per_agent_run=md.yahoo_max_calls_per_agent_run,
        max_retries=md.yahoo_max_retries,
    )


def crypto_history_profile(md: MarketDataLimits) -> ProviderThrottleProfile:
    return ProviderThrottleProfile(
        name="crypto_history",
        outbound_rpm=md.crypto_history_outbound_rpm,
        burst_calls=md.crypto_history_burst_calls,
        burst_window_seconds=md.crypto_history_burst_window_seconds,
        max_concurrency=md.crypto_history_max_concurrency,
        min_spacing_ms=md.crypto_history_min_spacing_ms,
        cache_ttl_seconds=md.crypto_history_cache_ttl_seconds,
        max_calls_per_agent_run=md.crypto_history_max_calls_per_agent_run,
        max_retries=md.crypto_history_max_retries,
    )


def news_profile(md: MarketDataLimits) -> ProviderThrottleProfile:
    return ProviderThrottleProfile(
        name="news",
        outbound_rpm=md.news_outbound_rpm,
        burst_calls=md.news_burst_calls,
        burst_window_seconds=md.news_burst_window_seconds,
        max_concurrency=md.news_max_concurrency,
        min_spacing_ms=md.news_min_spacing_ms,
        cache_ttl_seconds=md.news_cache_ttl_seconds,
        max_calls_per_agent_run=md.news_max_calls_per_agent_run,
        max_retries=md.news_max_retries,
    )


class TtlCache:
    """Thread-safe TTL cache for quotes / series / news blobs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            expires_at, value = item
            if now >= expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        with self._lock:
            self._store[key] = (time.monotonic() + ttl_seconds, value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


class AgentMarketDataBudget:
    """Per agent-run soft ceiling on outbound provider calls."""

    def __init__(self, *, max_calls_by_provider: dict[str, int]) -> None:
        self._max = dict(max_calls_by_provider)
        self._used: dict[str, int] = {k: 0 for k in max_calls_by_provider}
        self._lock = threading.Lock()

    def consume(self, provider: str, calls: int = 1) -> None:
        if calls <= 0:
            return
        with self._lock:
            ceiling = self._max.get(provider)
            if ceiling is None:
                return
            used = self._used.get(provider, 0)
            if used + calls > ceiling:
                raise MarketDataBudgetExceeded(
                    f"Agent market-data budget exceeded for {provider}: "
                    f"{used + calls}/{ceiling}"
                )
            self._used[provider] = used + calls

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {
                p: {"used": self._used.get(p, 0), "max": self._max.get(p, 0)}
                for p in self._max
            }


_budget_ctx: ContextVar[AgentMarketDataBudget | None] = ContextVar(
    "market_data_agent_budget",
    default=None,
)


def start_agent_market_budget(limits: TechnicalLimits | None = None) -> AgentMarketDataBudget:
    """
    Bind a soft outbound budget for the current agent run (contextvar).
    CrewAI orchestration should call this once at run start.
    """
    md = (limits or TechnicalLimits()).market_data
    budget = AgentMarketDataBudget(
        max_calls_by_provider={
            "yahoo": md.yahoo_max_calls_per_agent_run,
            "crypto_history": md.crypto_history_max_calls_per_agent_run,
            "news": md.news_max_calls_per_agent_run,
        }
    )
    _budget_ctx.set(budget)
    return budget


def clear_agent_market_budget() -> None:
    _budget_ctx.set(None)


def get_agent_market_budget() -> AgentMarketDataBudget | None:
    return _budget_ctx.get()


class ProviderGate:
    """RPM + sliding burst + min spacing + concurrency for one provider."""

    def __init__(self, profile: ProviderThrottleProfile) -> None:
        self.profile = profile
        self._lock = threading.Lock()
        self._last_call_at = 0.0
        self._tokens = float(profile.burst_calls)
        self._tokens_updated_at = time.monotonic()
        self._burst_times: deque[float] = deque()
        self._semaphore = threading.Semaphore(profile.max_concurrency)

    def reconfigure(self, profile: ProviderThrottleProfile) -> None:
        with self._lock:
            old_conc = self.profile.max_concurrency
            self.profile = profile
            self._tokens = min(self._tokens, float(profile.burst_calls))
            if profile.max_concurrency != old_conc:
                # Best-effort: new limit applies to subsequent acquires.
                self._semaphore = threading.Semaphore(profile.max_concurrency)

    def _refill_locked(self, now: float) -> None:
        rpm = max(1, self.profile.outbound_rpm)
        rate_per_sec = rpm / 60.0
        elapsed = max(0.0, now - self._tokens_updated_at)
        self._tokens = min(
            float(self.profile.burst_calls),
            self._tokens + elapsed * rate_per_sec,
        )
        self._tokens_updated_at = now

    def _wait_seconds_locked(self, now: float) -> float:
        self._refill_locked(now)
        wait = 0.0

        spacing = self.profile.min_spacing_ms / 1000.0
        if spacing > 0 and self._last_call_at > 0:
            wait = max(wait, spacing - (now - self._last_call_at))

        window = float(self.profile.burst_window_seconds)
        while self._burst_times and self._burst_times[0] <= now - window:
            self._burst_times.popleft()
        if len(self._burst_times) >= self.profile.burst_calls:
            wait = max(wait, window - (now - self._burst_times[0]))

        if self._tokens < 1.0:
            rpm = max(1, self.profile.outbound_rpm)
            wait = max(wait, (1.0 - self._tokens) / (rpm / 60.0))

        return max(0.0, wait)

    def _reserve_rate_unit(self, *, max_wait_seconds: float) -> None:
        deadline = time.monotonic() + max_wait_seconds
        while True:
            with self._lock:
                now = time.monotonic()
                wait = self._wait_seconds_locked(now)
                if wait <= 0:
                    self._tokens = max(0.0, self._tokens - 1.0)
                    self._last_call_at = now
                    self._burst_times.append(now)
                    return
            if time.monotonic() + wait > deadline:
                raise MarketDataThrottleError(
                    f"{self.profile.name} rate-limit wait timed out"
                )
            time.sleep(wait)

    def acquire(self, *, rate_units: int = 1, max_wait_seconds: float = 60.0) -> None:
        """
        Reserve `rate_units` against RPM/burst/spacing, then take one concurrency slot.
        Batch HTTP (e.g. multi-ticker download) should pass rate_units=1.
        """
        if rate_units <= 0:
            rate_units = 1

        budget = get_agent_market_budget()
        if budget is not None:
            budget.consume(self.profile.name, rate_units)

        for _ in range(rate_units):
            self._reserve_rate_unit(max_wait_seconds=max_wait_seconds)

        timeout = max(0.05, max_wait_seconds)
        if not self._semaphore.acquire(timeout=timeout):
            raise MarketDataThrottleError(
                f"{self.profile.name} concurrency wait timed out"
            )

    def release(self) -> None:
        self._semaphore.release()

    @contextmanager
    def slot(self, *, rate_units: int = 1) -> Iterator[None]:
        self.acquire(rate_units=rate_units)
        try:
            yield
        finally:
            self.release()


class MarketDataThrottleRegistry:
    """Process-wide gates + shared TTL cache keyed by provider."""

    def __init__(self) -> None:
        self.cache = TtlCache()
        self._lock = threading.Lock()
        self._gates: dict[str, ProviderGate] = {}
        self._limits = TechnicalLimits().market_data
        self._ensure_gates_locked()

    def _ensure_gates_locked(self) -> None:
        for profile in (
            yahoo_profile(self._limits),
            crypto_history_profile(self._limits),
            news_profile(self._limits),
        ):
            existing = self._gates.get(profile.name)
            if existing is None:
                self._gates[profile.name] = ProviderGate(profile)
            else:
                existing.reconfigure(profile)

    def configure(self, limits: TechnicalLimits | MarketDataLimits | None) -> None:
        if limits is None:
            md = TechnicalLimits().market_data
        elif isinstance(limits, TechnicalLimits):
            md = limits.market_data
        else:
            md = limits
        with self._lock:
            self._limits = md
            self._ensure_gates_locked()

    def gate(self, provider: str) -> ProviderGate:
        with self._lock:
            gate = self._gates.get(provider)
            if gate is None:
                raise KeyError(f"Unknown market-data provider: {provider}")
            return gate

    def limits(self) -> MarketDataLimits:
        with self._lock:
            return self._limits


_registry = MarketDataThrottleRegistry()


def get_throttle_registry() -> MarketDataThrottleRegistry:
    return _registry


def call_with_retries(
    *,
    provider: str,
    operation: str,
    fn: Callable[[], T],
    max_retries: int,
    rate_units: int = 1,
    is_rate_limited: Callable[[T], bool] | None = None,
) -> T:
    """Run `fn` under the provider gate with exponential backoff."""
    gate = _registry.gate(provider)
    last_exc: Exception | None = None
    attempts = max(1, max_retries + 1)
    for attempt in range(attempts):
        with gate.slot(rate_units=rate_units):
            try:
                result = fn()
                if is_rate_limited is not None and is_rate_limited(result):
                    raise MarketDataThrottleError(f"{provider} soft rate-limit signal")
                return result
            except Exception as exc:  # noqa: BLE001 — retry then soft-fail upstream
                last_exc = exc
                logger.warning(
                    "market_data_call_failed",
                    provider=provider,
                    operation=operation,
                    attempt=attempt + 1,
                    error=str(exc),
                )
        if attempt + 1 < attempts:
            time.sleep(min(8.0, float(2**attempt)))
    if last_exc is not None:
        raise last_exc
    raise MarketDataThrottleError(f"{provider} {operation} failed with no exception")
