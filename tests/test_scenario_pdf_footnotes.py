"""PDF render — institutional one-pager smoke tests."""

from __future__ import annotations

from datetime import datetime, timezone

from backend.app.schemas.scenario import ClientPortfolioMetrics
from backend.app.services.scenario_pdf import (
    build_scenario_pdf,
    format_scenario_summary_line,
    select_stats_rows,
)


def test_one_pager_pdf_renders_with_chart_and_cards() -> None:
    pdf = build_scenario_pdf(
        scenario_name="Tencent stock to plunge",
        client_alias="Acme Family",
        as_of=datetime(2026, 9, 28, tzinfo=timezone.utc),
        opti_folio=[
            {
                "ticker": "AAPL",
                "asset_class": "EQUITY",
                "weight_pct": 0.25,
                "notional_value": 250_000,
            },
            {
                "ticker": "MSFT",
                "asset_class": "EQUITY",
                "weight_pct": 0.20,
                "notional_value": 200_000,
            },
            {
                "ticker": "CASH",
                "asset_class": "CASH",
                "weight_pct": 0.55,
                "notional_value": 550_000,
            },
        ],
        opti_metrics=ClientPortfolioMetrics(
            expected_return=0.12,
            annualized_volatility=0.15,
            sharpe_ratio=1.2,
            max_drawdown=-0.08,
        ),
        base_metrics=ClientPortfolioMetrics(
            expected_return=0.10,
            annualized_volatility=0.18,
            sharpe_ratio=1.0,
            max_drawdown=-0.12,
        ),
        talking_points=[
            (
                "Performance statistics of the portfolio improved compared to the "
                "existing portfolio, with Sharpe ratio improving by 20%."
            ),
            "House view: Constructive (tone=risk_off, coin_temperature=2/5).",
            "Outlook: constructive on quality equities.",
        ],
        news_footnotes=["[1] AAPL — Reuters, Bloomberg"],
        performance_series={
            "lookback_window": "5Y",
            "label_base": "Starting Positions",
            "label_opti": "Optimized Portfolio",
            "base_total_return": 0.40,
            "opti_total_return": 0.55,
            "points": [
                {
                    "date": f"2020-01-{(i % 28) + 1:02d}",
                    "starting_positions": 100 + i * 0.4,
                    "optimized_portfolio": 100 + i * 0.7,
                }
                for i in range(30)
            ],
        },
    )
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 2000
    # Content streams are compressed — extract via pdftotext when available.
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    if shutil.which("pdftotext"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.pdf"
            path.write_bytes(pdf)
            page_text = subprocess.check_output(
                ["pdftotext", "-raw", str(path), "-"],
                text=True,
            )
        assert "Start peak" not in page_text
        assert "bps vs Starting" not in page_text
        assert "Scenario provided was for Tencent stock to plunge" in page_text
        assert "coin_temperature" not in page_text
        assert "tone=risk_off" not in page_text
        assert "risk off tone" in " ".join(page_text.split())
    else:
        assert (
            format_scenario_summary_line("Tencent stock to plunge")
            == "Scenario provided was for Tencent stock to plunge."
        )


def test_format_scenario_summary_line() -> None:
    assert (
        format_scenario_summary_line("Tencent stock to plunge")
        == "Scenario provided was for Tencent stock to plunge."
    )
    assert format_scenario_summary_line("Market scenario") is None
    assert (
        format_scenario_summary_line("Scenario provided was for an oil shock")
        == "Scenario provided was for an oil shock."
    )


def test_one_pager_pdf_stable_without_performance() -> None:
    pdf = build_scenario_pdf(
        scenario_name="x",
        client_alias="Client",
        opti_folio=[],
        opti_metrics=ClientPortfolioMetrics(),
        base_metrics=None,
        talking_points=[],
        performance_series=None,
    )
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 800


def test_select_stats_rows_prefers_commentary_and_improvements() -> None:
    rows = select_stats_rows(
        opti={
            "expected_return": 0.12,
            "annualized_volatility": 0.15,
            "sharpe_ratio": 1.2,
            "max_drawdown": -0.08,
            "sortino_ratio": 1.5,
        },
        base={
            "expected_return": 0.10,
            "annualized_volatility": 0.18,
            "sharpe_ratio": 1.0,
            "max_drawdown": -0.12,
            "sortino_ratio": 1.1,
        },
        opti_folio=[
            {"ticker": "AAPL", "notional_value": 100_000, "weight_pct": 1.0},
        ],
        performance_series={
            "lookback_window": "5Y",
            "base_total_return": 0.40,
            "opti_total_return": 0.55,
        },
        talking_points=[
            "Performance ... with Sharpe ratio improving by 20%.",
        ],
        max_rows=6,
    )
    labels = [r[0] for r in rows]
    assert labels[0] == "Portfolio Value"
    assert labels[1].startswith("Lookback Return")
    # Commentary-mentioned Sharpe should appear, and be accented as improved.
    sharpe = next(r for r in rows if r[0] == "Sharpe Ratio")
    assert sharpe[2] is True
    # Lookback outperformance is accented.
    assert rows[1][2] is True
