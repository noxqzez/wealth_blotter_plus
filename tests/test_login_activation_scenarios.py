"""Login / activation mock scenarios for exec-monitor marketing narratives."""

from __future__ import annotations

from frontend_streamlit.exec_monitor.data import compute_kpis
from frontend_streamlit.exec_monitor.mock_data import LOGIN_SCENARIOS, mock_bundle


def test_login_scenarios_span_activation_band() -> None:
    rates: dict[str, float] = {}
    for name in LOGIN_SCENARIOS:
        bundle = mock_bundle(days=30, login_scenario=name)
        kpis = compute_kpis(bundle)
        rates[name] = float(kpis["activation_rate_pct"])
        assert kpis["total_onboarded"] == 30
        assert "activation_rate_pct" in kpis

    pre = rates["Pre-SSO (password / fragmented)"]
    post = rates["Post-SSO (firm IdP)"]
    byok = rates["BYOK (advisor API keys)"]
    assert pre < byok < post
    assert pre <= 35
    assert post >= 75
