"""Deterministic portfolio / client data factory for tests and demo seeding."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid5

from faker import Faker

# Stable namespace so seed=42 always yields the same UUIDs across machines.
_FACTORY_NS = UUID("a1b2c3d4-e5f6-4789-a012-3456789abcde")

RiskProfile = Literal[1, 2, 3, 4, 5]
AssetClass = Literal["EQUITY", "CRYPTO", "CASH"]

# Liquid names used in marketing screenshots and golden tests.
_EQUITY_UNIVERSE: tuple[tuple[str, str, float], ...] = (
    ("AAPL", "Apple Inc", 190.0),
    ("MSFT", "Microsoft", 420.0),
    ("JPM", "JPMorgan Chase", 198.0),
    ("NVDA", "NVIDIA", 875.0),
    ("0700.HK", "Tencent", 380.0),
    ("9988.HK", "Alibaba", 88.0),
)
_CRYPTO_UNIVERSE: tuple[tuple[str, str, float], ...] = (
    ("BTC-USD", "Bitcoin", 65_000.0),
    ("ETH-USD", "Ethereum", 3_400.0),
)
_CASH_UNIVERSE: tuple[tuple[str, str, float], ...] = (
    ("USD", "US Dollar", 1.0),
    ("HKD", "Hong Kong Dollar", 1.0),
)

# Fixed demo advisor identities (no advisors table — JWT `sub` / staged_trades.advisor_id).
DEMO_ADVISOR_IDS: tuple[UUID, ...] = tuple(
    uuid5(_FACTORY_NS, f"advisor:{i}") for i in range(1, 6)
)


class FixtureKind(str, Enum):
    VALID = "valid"
    EDGE_OVERWEIGHT = "edge_overweight"
    ADVERSARIAL_TICKER = "adversarial_ticker"
    ADVERSARIAL_NEGATIVE_QTY = "adversarial_negative_qty"
    EDGE_TOO_FEW_POSITIONS = "edge_too_few_positions"


@dataclass(frozen=True, slots=True)
class ClientSpec:
    id: UUID
    client_alias: str
    display_name: str
    risk_profile: RiskProfile
    suitability_score: float
    advisor_id: UUID


@dataclass(frozen=True, slots=True)
class PositionSpec:
    id: UUID
    ticker: str
    asset_class: AssetClass
    quantity: float
    spot_price: float
    short_name: str
    notional_value: float
    current_weight_pct: float
    currency: str = "USD"
    price_status: str = "VALID"


@dataclass(frozen=True, slots=True)
class PortfolioSpec:
    id: UUID
    client_id: UUID
    client_alias: str
    advisor_id: UUID
    cash_balance: float
    currency: str
    folio_stage: str
    import_hitl: bool
    positions: tuple[PositionSpec, ...]
    kind: FixtureKind = FixtureKind.VALID
    risk_profile: RiskProfile = 3

    @property
    def total_notional(self) -> float:
        return float(sum(p.notional_value for p in self.positions))


@dataclass(frozen=True, slots=True)
class DemoBundle:
    """Full demo graph: advisors → clients → portfolios → positions."""

    seed: int
    advisors: tuple[UUID, ...]
    clients: tuple[ClientSpec, ...]
    portfolios: tuple[PortfolioSpec, ...]
    meta: dict[str, Any] = field(default_factory=dict)

    def to_summary(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "n_advisors": len(self.advisors),
            "n_clients": len(self.clients),
            "n_portfolios": len(self.portfolios),
            "n_positions": sum(len(p.positions) for p in self.portfolios),
            "kinds": sorted({p.kind.value for p in self.portfolios}),
        }


def _det_uuid(seed: int, *parts: str | int) -> UUID:
    key = ":".join([str(seed), *[str(p) for p in parts]])
    return uuid5(_FACTORY_NS, key)


def _faker(seed: int) -> Faker:
    fake = Faker()
    Faker.seed(seed)
    fake.seed_instance(seed)
    return fake


def _risk_for_index(i: int) -> RiskProfile:
    # Cycle 1–5; stored on clients.risk_profile for future advisor checks.
    return ((i - 1) % 5) + 1  # type: ignore[return-value]


def _suitability(profile: RiskProfile, rng_unit: float) -> float:
    base = {1: 0.78, 2: 0.82, 3: 0.85, 4: 0.80, 5: 0.72}[profile]
    return round(min(0.99, max(0.55, base + (rng_unit - 0.5) * 0.1)), 3)


class PortfolioFactory:
    """Generate deterministic clients, portfolios, and adversarial fixtures."""

    def __init__(self, *, seed: int = 42) -> None:
        self.seed = int(seed)
        self._fake = _faker(self.seed)

    def make_demo_bundle(
        self,
        *,
        n_advisors: int = 5,
        clients_per_advisor: int = 3,
        include_edge_cases: bool = False,
    ) -> DemoBundle:
        """
        Build marketing-ready data: balanced BASE portfolios by default.

        When ``include_edge_cases`` is True, the last client per advisor gets an
        overweight fixture (for guardrail demos) — not used for pristine screenshots.
        """
        n_advisors = max(1, n_advisors)
        advisors = tuple(
            _det_uuid(self.seed, "advisor", i) for i in range(1, n_advisors + 1)
        )
        clients: list[ClientSpec] = []
        portfolios: list[PortfolioSpec] = []
        idx = 0
        for a_i, advisor_id in enumerate(advisors):
            for c_i in range(clients_per_advisor):
                idx += 1
                client = self.make_client(index=idx, advisor_id=advisor_id)
                clients.append(client)
                use_edge = include_edge_cases and c_i == clients_per_advisor - 1
                if use_edge:
                    folio = self.make_edge_overweight_portfolio(
                        client=client, index=idx
                    )
                else:
                    folio = self.make_balanced_portfolio(client=client, index=idx)
                portfolios.append(folio)

        return DemoBundle(
            seed=self.seed,
            advisors=advisors,
            clients=tuple(clients),
            portfolios=tuple(portfolios),
            meta={"include_edge_cases": include_edge_cases},
        )

    def make_client(self, *, index: int, advisor_id: UUID | None = None) -> ClientSpec:
        profile = _risk_for_index(index)
        name = self._fake.name()
        alias = f"DEMO_{self.seed}_{index:02d}_{name.split()[-1].upper()[:8]}"
        aid = advisor_id or DEMO_ADVISOR_IDS[index % len(DEMO_ADVISOR_IDS)]
        return ClientSpec(
            id=_det_uuid(self.seed, "client", index),
            client_alias=alias[:64],
            display_name=name,
            risk_profile=profile,
            suitability_score=_suitability(profile, self._fake.pyfloat(min_value=0, max_value=1)),
            advisor_id=aid,
        )

    def make_balanced_portfolio(
        self, *, client: ClientSpec, index: int
    ) -> PortfolioSpec:
        """5–8 positions: equities + optional crypto + cash; weights sum to ~1.0."""
        n_eq = 4 + (index % 2)  # 4..5 equities so total positions always ≥ 5
        equities = list(_EQUITY_UNIVERSE[:n_eq])
        include_crypto = client.risk_profile >= 4
        cryptos = list(_CRYPTO_UNIVERSE[:1]) if include_crypto else []
        # Always keep USD + HKD cash when any HK name is present; else USD + second USD sleeve
        # via an extra US equity so normalize/import floors (≥5) are satisfied.
        cash = [_CASH_UNIVERSE[0]]
        if any(t.endswith(".HK") for t, _, _ in equities):
            cash.append(_CASH_UNIVERSE[1])
        elif not include_crypto and n_eq < 5:
            equities = list(_EQUITY_UNIVERSE[:5])
            n_eq = 5

        raw: list[tuple[str, AssetClass, str, float, float]] = []
        # Target weights by sleeve before cash residual.
        eq_budget = 0.70 if include_crypto else 0.85
        crypto_budget = 0.10 if include_crypto else 0.0
        for i, (ticker, short, px) in enumerate(equities):
            w = eq_budget / len(equities)
            qty = round((100_000.0 * w) / px, 4)
            raw.append((ticker, "EQUITY", short, qty, px))
        for ticker, short, px in cryptos:
            w = crypto_budget / len(cryptos)
            qty = round((100_000.0 * w) / px, 6)
            raw.append((ticker, "CRYPTO", short, qty, px))
        # Cash residual notionals so total ≈ 100k
        equity_crypto_notional = sum(q * p for _, _, _, q, p in raw)
        cash_total = max(5_000.0, 100_000.0 - equity_crypto_notional)
        per_cash = cash_total / len(cash)
        for ticker, short, px in cash:
            raw.append((ticker, "CASH", short, round(per_cash / px, 2), px))

        positions = self._positions_from_raw(index=index, rows=raw)
        return PortfolioSpec(
            id=_det_uuid(self.seed, "portfolio", index),
            client_id=client.id,
            client_alias=client.client_alias,
            advisor_id=client.advisor_id,
            cash_balance=float(
                sum(p.notional_value for p in positions if p.asset_class == "CASH")
            ),
            currency="USD",
            folio_stage="BASE",
            import_hitl=False,
            positions=positions,
            kind=FixtureKind.VALID,
            risk_profile=client.risk_profile,
        )

    def make_edge_overweight_portfolio(
        self, *, client: ClientSpec, index: int
    ) -> PortfolioSpec:
        """Single-name weight ~45% — breaches default max_single_name_weight (25%)."""
        raw: list[tuple[str, AssetClass, str, float, float]] = [
            ("AAPL", "EQUITY", "Apple Inc", 250.0, 190.0),  # ~47.5k
            ("MSFT", "EQUITY", "Microsoft", 20.0, 420.0),
            ("JPM", "EQUITY", "JPMorgan Chase", 30.0, 198.0),
            ("USD", "CASH", "US Dollar", 8_000.0, 1.0),
            ("BTC-USD", "CRYPTO", "Bitcoin", 0.05, 65_000.0),
        ]
        positions = self._positions_from_raw(index=index, rows=raw)
        return PortfolioSpec(
            id=_det_uuid(self.seed, "portfolio", "edge", index),
            client_id=client.id,
            client_alias=client.client_alias,
            advisor_id=client.advisor_id,
            cash_balance=8_000.0,
            currency="USD",
            folio_stage="BASE",
            import_hitl=True,
            positions=positions,
            kind=FixtureKind.EDGE_OVERWEIGHT,
            risk_profile=client.risk_profile,
        )

    def make_adversarial_import_rows(self, *, kind: FixtureKind) -> list[dict[str, Any]]:
        """Raw import-shaped dicts for API / normalize golden tests."""
        alias = f"ADV_{self.seed}_{kind.value}"
        base = [
            {
                "client_alias": alias,
                "asset_class": "EQUITY",
                "ticker": "AAPL",
                "quantity": 10.0,
                "currency": "USD",
            },
            {
                "client_alias": alias,
                "asset_class": "EQUITY",
                "ticker": "MSFT",
                "quantity": 5.0,
                "currency": "USD",
            },
            {
                "client_alias": alias,
                "asset_class": "EQUITY",
                "ticker": "JPM",
                "quantity": 8.0,
                "currency": "USD",
            },
            {
                "client_alias": alias,
                "asset_class": "EQUITY",
                "ticker": "NVDA",
                "quantity": 2.0,
                "currency": "USD",
            },
            {
                "client_alias": alias,
                "asset_class": "CASH",
                "ticker": "USD",
                "quantity": 1000.0,
                "currency": "USD",
            },
        ]
        if kind == FixtureKind.ADVERSARIAL_TICKER:
            base[0]["ticker"] = "!!!NOT_A_TICKER$$$"
            return base
        if kind == FixtureKind.ADVERSARIAL_NEGATIVE_QTY:
            base[1]["quantity"] = -50.0
            return base
        if kind == FixtureKind.EDGE_TOO_FEW_POSITIONS:
            return base[:2]
        if kind == FixtureKind.EDGE_OVERWEIGHT:
            # Valid shape for import; overweight is a post-weight compliance concern.
            base[0]["quantity"] = 10_000.0
            return base
        return base

    def make_golden_agent_prompts(self) -> list[dict[str, Any]]:
        """Prompts / contexts for drift tests (anonymized — no PII)."""
        return [
            {
                "id": "golden_balanced",
                "client_ref": f"cref-{self.seed}-balanced",
                "notes": "Rebalance toward moderate equity sleeve; keep cash buffer.",
                "expect_route_min_confidence": 0.85,
            },
            {
                "id": "golden_overweight_breach",
                "client_ref": f"cref-{self.seed}-overwt",
                "notes": "Single name exceeds mandate; flag compliance.",
                "expect_route_min_confidence": 0.0,
            },
            {
                "id": "adversarial_nonsense",
                "client_ref": f"cref-{self.seed}-nonsense",
                "notes": "asdkfjh!!! buy purple unicorn futures at -999 weight",
                "expect_hitl": True,
            },
        ]

    def _positions_from_raw(
        self,
        *,
        index: int,
        rows: list[tuple[str, AssetClass, str, float, float]],
    ) -> tuple[PositionSpec, ...]:
        notionals = [qty * px for _, _, _, qty, px in rows]
        total = sum(notionals) or 1.0
        out: list[PositionSpec] = []
        for i, (ticker, ac, short, qty, px) in enumerate(rows):
            notional = round(qty * px, 2)
            weight = round(notional / total, 6)
            ccy = "HKD" if ticker.endswith(".HK") or ticker == "HKD" else "USD"
            out.append(
                PositionSpec(
                    id=_det_uuid(self.seed, "pos", index, i, ticker),
                    ticker=ticker,
                    asset_class=ac,
                    quantity=float(qty),
                    spot_price=float(px),
                    short_name=short,
                    notional_value=notional,
                    current_weight_pct=weight,
                    currency=ccy,
                )
            )
        return tuple(out)


def portfolio_to_db_rows(portfolio: PortfolioSpec) -> dict[str, Any]:
    """Shape ready for service-role upsert (portfolio + positions)."""
    return {
        "portfolio": {
            "id": str(portfolio.id),
            "client_id": str(portfolio.client_id),
            "cash_balance": portfolio.cash_balance,
            "currency": portfolio.currency,
            "folio_stage": portfolio.folio_stage,
            "import_hitl": portfolio.import_hitl,
            "draft_folio": [],
        },
        "positions": [
            {
                "id": str(p.id),
                "portfolio_id": str(portfolio.id),
                "ticker": p.ticker,
                "asset_class": p.asset_class,
                "quantity": p.quantity,
                "spot_price": p.spot_price,
                "short_name": p.short_name,
                "notional_value": p.notional_value,
                "current_weight_pct": p.current_weight_pct,
                "currency": p.currency,
                "price_status": p.price_status,
            }
            for p in portfolio.positions
        ],
        "client": {
            "id": str(portfolio.client_id),
            "client_alias": portfolio.client_alias,
            "risk_profile": int(portfolio.risk_profile),
        },
        "factory_meta": {
            "advisor_id": str(portfolio.advisor_id),
            "risk_profile": portfolio.risk_profile,
            "kind": portfolio.kind.value,
        },
    }


def client_to_dict(client: ClientSpec) -> dict[str, Any]:
    return asdict(client)
