#!/usr/bin/env python3
"""Seed Supabase with deterministic demo clients / portfolios / positions.

Usage (from repo root):

    export SUPABASE_URL=...
    export SUPABASE_SERVICE_ROLE_KEY=...
    uv run python scripts/seed_demo_data.py
    uv run python scripts/seed_demo_data.py --seed 42 --dry-run

Requires service-role credentials. Never prints or logs the service key.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from factories.portfolio_factory import (  # noqa: E402
    PortfolioFactory,
    portfolio_to_db_rows,
)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _client():
    from backend.app.core.supabase import get_service_supabase

    return get_service_supabase()


def seed_demo(
    *,
    seed: int = 42,
    n_advisors: int = 5,
    clients_per_advisor: int = 3,
    dry_run: bool = False,
    include_edge_cases: bool = False,
) -> dict[str, Any]:
    factory = PortfolioFactory(seed=seed)
    bundle = factory.make_demo_bundle(
        n_advisors=n_advisors,
        clients_per_advisor=clients_per_advisor,
        include_edge_cases=include_edge_cases,
    )
    summary = bundle.to_summary()
    _log(f"[seed_demo] seed={seed} dry_run={dry_run} summary={summary}")

    if dry_run:
        for folio in bundle.portfolios[:3]:
            _log(
                f"  sample {folio.client_alias}: "
                f"{len(folio.positions)} positions, "
                f"notional≈{folio.total_notional:,.0f}, kind={folio.kind.value}"
            )
        _log("[seed_demo] dry-run complete — no writes")
        return summary

    sb = _client()
    clients_ok = 0
    portfolios_ok = 0
    positions_ok = 0

    for client in bundle.clients:
        row = {
            "id": str(client.id),
            "client_alias": client.client_alias,
            "risk_profile": int(client.risk_profile),
        }
        sb.table("clients").upsert(row, on_conflict="id").execute()
        clients_ok += 1

    for folio in bundle.portfolios:
        shaped = portfolio_to_db_rows(folio)
        sb.table("portfolios").upsert(
            shaped["portfolio"], on_conflict="id"
        ).execute()
        portfolios_ok += 1
        # Replace positions for idempotent re-seed of this portfolio.
        sb.table("portfolio_positions").delete().eq(
            "portfolio_id", str(folio.id)
        ).execute()
        if shaped["positions"]:
            sb.table("portfolio_positions").insert(shaped["positions"]).execute()
            positions_ok += len(shaped["positions"])
        _log(
            f"  ✓ {folio.client_alias} "
            f"(risk_profile={folio.risk_profile}) — {len(folio.positions)} positions"
        )

    _log(
        f"[seed_demo] done — clients={clients_ok} "
        f"portfolios={portfolios_ok} positions={positions_ok}"
    )
    _log(
        "Open the Excel add-in (or Streamlit monitor) against this project — "
        "demo aliases are DEMO_{seed}_*."
    )
    return {
        **summary,
        "clients_written": clients_ok,
        "portfolios_written": portfolios_ok,
        "positions_written": positions_ok,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--advisors", type=int, default=5)
    parser.add_argument("--clients-per-advisor", type=int, default=3)
    parser.add_argument(
        "--include-edge-cases",
        action="store_true",
        help="Include overweight portfolios (skip for pristine marketing screenshots)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan only; do not write to Supabase",
    )
    args = parser.parse_args(argv)

    if not args.dry_run:
        if not os.environ.get("SUPABASE_URL") or not os.environ.get(
            "SUPABASE_SERVICE_ROLE_KEY"
        ):
            _log(
                "ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set "
                "(or use --dry-run)."
            )
            return 1

    seed_demo(
        seed=args.seed,
        n_advisors=args.advisors,
        clients_per_advisor=args.clients_per_advisor,
        dry_run=args.dry_run,
        include_edge_cases=args.include_edge_cases,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
