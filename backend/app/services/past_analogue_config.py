"""Past Analogue feature-flag helpers (easy rollback: keep flags false).

When ``past_analogue`` is false or phase is past pilot, all Past Analogue
code paths no-op (no Tavily / no extract). When on, admitted analogues
re-opti with trading rules, then blend with the strategist Opti Folio
(``past_analogue_mix``, default 0.5) before applying live weights.
"""

from __future__ import annotations

from typing import Any, Mapping

# Pilot phases that may enable Past Analogue. Post-pilot → hard off.
_PILOT_PHASES = frozenset({"pilot", "phase1", "phase2"})

DEFAULT_ARCHIVE_YEARS = 8
DEFAULT_ARCHIVE_DAYS = 2920  # 8 * 365
DEFAULT_TOP_K = 5
DEFAULT_SIMILARITY_THRESHOLD = 0.35
DEFAULT_TRAILING_BLEND = 0.40
DEFAULT_MAX_WINDOW_DAYS = 180
DEFAULT_SEARCH_TIMEOUT_MS = 15_000
# Fraction of analogue-conditioned opti in the applied mix (rest = strategist opti).
DEFAULT_ANALOGUE_MIX = 0.50


def past_analogue_allowed(scenario_cfg: Mapping[str, Any] | None) -> bool:
    """True only during pilot-capable phases (config gate for post-pilot lock)."""
    cfg = dict(scenario_cfg or {})
    phase = str(cfg.get("phase") or "phase1").strip().lower()
    if cfg.get("past_analogue_allowed") is False:
        return False
    # Explicit post-pilot lock
    if phase in {"post_pilot", "production", "ga"}:
        return False
    return phase in _PILOT_PHASES


def past_analogue_enabled(
    feature_flags: Mapping[str, Any] | None,
    scenario_cfg: Mapping[str, Any] | None = None,
) -> bool:
    """Master switch: live flag AND pilot gate. Default off."""
    if not past_analogue_allowed(scenario_cfg):
        return False
    flags = dict(feature_flags or {})
    return bool(flags.get("past_analogue", False))


def past_analogue_apply_enabled(
    feature_flags: Mapping[str, Any] | None,
    scenario_cfg: Mapping[str, Any] | None = None,
) -> bool:
    """Apply analogue-tilted weights to live opti when Past Analogue is on.

    Same master switch as ``past_analogue_enabled`` — turn the feature off to
    skip Tavily and leave live weights unchanged. Trading / compliance rules
    still run inside the analogue re-opti (``propose_opti_folio``).
    """
    return past_analogue_enabled(feature_flags, scenario_cfg)


def past_analogue_knobs(scenario_cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    cfg = dict(scenario_cfg or {})
    return {
        "archive_years": int(cfg.get("past_analogue_archive_years") or DEFAULT_ARCHIVE_YEARS),
        "archive_days": int(
            cfg.get("past_analogue_archive_days")
            or int(cfg.get("past_analogue_archive_years") or DEFAULT_ARCHIVE_YEARS) * 365
        ),
        "top_k": int(cfg.get("past_analogue_top_k") or DEFAULT_TOP_K),
        "similarity_threshold": float(
            cfg.get("past_analogue_similarity_threshold") or DEFAULT_SIMILARITY_THRESHOLD
        ),
        "trailing_blend": float(
            cfg.get("past_analogue_trailing_blend") or DEFAULT_TRAILING_BLEND
        ),
        "max_window_days": int(
            cfg.get("past_analogue_max_window_days") or DEFAULT_MAX_WINDOW_DAYS
        ),
        "search_timeout_ms": int(
            cfg.get("past_analogue_search_timeout_ms") or DEFAULT_SEARCH_TIMEOUT_MS
        ),
        "analogue_mix": float(
            min(
                1.0,
                max(
                    0.0,
                    float(
                        cfg.get("past_analogue_mix")
                        if cfg.get("past_analogue_mix") is not None
                        else DEFAULT_ANALOGUE_MIX
                    ),
                ),
            )
        ),
        "web_search": bool(
            (cfg.get("feature_flags") or {}).get("past_analogue_web_search", True)
        ),
    }
