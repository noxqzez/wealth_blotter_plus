"""Past Analogue admit gate, 8Y reweight, shadow optimize, ΔW decomposition.

All math is deterministic. Fail-open wrappers live in scenario_run.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import structlog

from backend.app.schemas.scenario import (
    PastAnalogueArtifact,
    PastAnaloguePeriodHit,
    ScenarioParams,
    StrategyView,
    WeightDecomposition,
    WeightDeltaComponent,
)
from backend.app.services.past_analogue_config import (
    DEFAULT_ANALOGUE_MIX,
    DEFAULT_MAX_WINDOW_DAYS,
    DEFAULT_SIMILARITY_THRESHOLD,
    DEFAULT_TOP_K,
    DEFAULT_TRAILING_BLEND,
)
from backend.app.services.past_analogue_search import run_past_analogue_search
from backend.app.services.scenario_optimizer import (
    DEFAULT_RISK_AVERSION,
    _normalize_asset_class,
    propose_opti_folio,
    weights_to_opti_positions,
)

logger = structlog.get_logger(__name__)

_MIN_WINDOW_DAYS = 20


def _parse_date(raw: str) -> date | None:
    text = (raw or "").strip()[:32]
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def admit_hits(
    hits: Sequence[PastAnaloguePeriodHit],
    returns: pd.DataFrame,
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    top_k: int = DEFAULT_TOP_K,
    max_window_days: int = DEFAULT_MAX_WINDOW_DAYS,
) -> tuple[list[PastAnaloguePeriodHit], list[PastAnaloguePeriodHit]]:
    """Validate / clip hits against Yahoo returns index. Returns (all_annotated, admitted)."""
    annotated: list[PastAnaloguePeriodHit] = []
    if returns is None or returns.empty:
        for h in hits:
            annotated.append(
                h.model_copy(
                    update={"admitted": False, "reject_reason": "empty_returns_panel"}
                )
            )
        return annotated, []

    idx = pd.DatetimeIndex(returns.index).tz_localize(None)
    panel_start = idx.min().date()
    panel_end = idx.max().date()

    for h in hits:
        start = _parse_date(h.start_date)
        end = _parse_date(h.end_date)
        if start is None or end is None:
            annotated.append(
                h.model_copy(
                    update={"admitted": False, "reject_reason": "invalid_dates"}
                )
            )
            continue
        if end < start:
            start, end = end, start
        if (end - start).days > max_window_days:
            end = start + timedelta(days=max_window_days)
        # Clip to Yahoo panel
        clip_start = max(start, panel_start)
        clip_end = min(end, panel_end)
        if clip_end < clip_start:
            annotated.append(
                h.model_copy(
                    update={
                        "admitted": False,
                        "reject_reason": "outside_yahoo_panel",
                        "clipped_start": clip_start.isoformat(),
                        "clipped_end": clip_end.isoformat(),
                    }
                )
            )
            continue
        mask = (idx.date >= clip_start) & (idx.date <= clip_end)
        n_days = int(mask.sum())
        if n_days < _MIN_WINDOW_DAYS:
            annotated.append(
                h.model_copy(
                    update={
                        "admitted": False,
                        "reject_reason": f"insufficient_bars:{n_days}",
                        "clipped_start": clip_start.isoformat(),
                        "clipped_end": clip_end.isoformat(),
                    }
                )
            )
            continue
        # Coverage factor scales model similarity (no invented path score yet).
        coverage = min(1.0, n_days / float(max_window_days))
        validated = float(max(0.0, min(1.0, h.model_similarity * (0.5 + 0.5 * coverage))))
        if validated < threshold:
            annotated.append(
                h.model_copy(
                    update={
                        "admitted": False,
                        "reject_reason": f"below_threshold:{validated:.3f}",
                        "validated_similarity": validated,
                        "clipped_start": clip_start.isoformat(),
                        "clipped_end": clip_end.isoformat(),
                    }
                )
            )
            continue
        annotated.append(
            h.model_copy(
                update={
                    "admitted": True,
                    "reject_reason": None,
                    "validated_similarity": validated,
                    "clipped_start": clip_start.isoformat(),
                    "clipped_end": clip_end.isoformat(),
                }
            )
        )

    admitted = [h for h in annotated if h.admitted]
    admitted.sort(key=lambda x: float(x.validated_similarity or 0.0), reverse=True)
    admitted = admitted[:top_k]
    # Re-mark overflow as rejected for audit clarity
    kept_keys = {(a.start_date, a.end_date, a.label) for a in admitted}
    final_annotated: list[PastAnaloguePeriodHit] = []
    for h in annotated:
        key = (h.start_date, h.end_date, h.label)
        if h.admitted and key not in kept_keys:
            final_annotated.append(
                h.model_copy(
                    update={"admitted": False, "reject_reason": "top_k_overflow"}
                )
            )
        else:
            final_annotated.append(h)
    return final_annotated, admitted


def reweight_returns(
    returns: pd.DataFrame,
    admitted: Sequence[PastAnaloguePeriodHit],
    *,
    trailing_blend: float = DEFAULT_TRAILING_BLEND,
) -> pd.DataFrame:
    """Importance-weight rows inside admitted windows; keep trailing mass.

    Implementation: upsample admitted window rows by integer repeats proportional
    to validated_similarity, then concat a trailing slice so blend ≈ trailing_blend.
    """
    if returns is None or returns.empty or not admitted:
        return returns

    blend = float(min(0.95, max(0.05, trailing_blend)))
    idx = pd.DatetimeIndex(returns.index).tz_localize(None)
    parts: list[pd.DataFrame] = []
    weights: list[float] = []
    for h in admitted:
        cs = _parse_date(h.clipped_start or h.start_date)
        ce = _parse_date(h.clipped_end or h.end_date)
        if cs is None or ce is None:
            continue
        mask = (idx.date >= cs) & (idx.date <= ce)
        chunk = returns.loc[mask]
        if chunk.empty:
            continue
        sim = float(h.validated_similarity or h.model_similarity or 0.0)
        parts.append(chunk)
        weights.append(max(sim, 0.05))

    if not parts:
        return returns

    # Trailing slice (~same length as mean admitted window)
    mean_len = int(np.mean([len(p) for p in parts]))
    mean_len = max(_MIN_WINDOW_DAYS, mean_len)
    trailing = returns.iloc[-mean_len:]

    # Build weighted concatenation via repeats
    analogue_mass = 1.0 - blend
    wsum = sum(weights) or 1.0
    frames: list[pd.DataFrame] = []
    for chunk, w in zip(parts, weights, strict=True):
        share = analogue_mass * (w / wsum)
        repeats = max(1, int(round(share / analogue_mass * 4)))
        for _ in range(repeats):
            frames.append(chunk)
    # Trailing repeats
    t_rep = max(1, int(round(blend / analogue_mass * 4))) if analogue_mass > 0 else 1
    for _ in range(t_rep):
        frames.append(trailing)

    out = pd.concat(frames, axis=0)
    # Drop duplicate index labels that confuse some cov estimators by resetting
    out = out.copy()
    out.index = pd.RangeIndex(len(out))
    return out


def _delta_maps(
    base: Mapping[str, float],
    nxt: Mapping[str, float],
) -> dict[str, float]:
    keys = set(base) | set(nxt)
    return {k: float(nxt.get(k, 0.0) - base.get(k, 0.0)) for k in sorted(keys)}


def _l1(delta: Mapping[str, float]) -> float:
    return float(sum(abs(v) for v in delta.values()))


def blend_strategist_analogue_weights(
    strategist: Mapping[str, float],
    analogue: Mapping[str, float],
    *,
    analogue_mix: float = DEFAULT_ANALOGUE_MIX,
) -> dict[str, float]:
    """Convex mix: ``(1-mix)*strategist + mix*analogue``, then renormalize.

    Both inputs already ran through trading/compliance constraints; a convex
    combination preserves linear bound feasibility. ``analogue_mix=0`` keeps
    strategist only; ``1`` is full analogue replace.
    """
    mix = float(min(1.0, max(0.0, analogue_mix)))
    keys = sorted(set(strategist) | set(analogue))
    if not keys:
        return {}
    if mix <= 0.0:
        out = {k: float(strategist.get(k, 0.0)) for k in keys}
    elif mix >= 1.0:
        out = {k: float(analogue.get(k, 0.0)) for k in keys}
    else:
        keep = 1.0 - mix
        out = {
            k: keep * float(strategist.get(k, 0.0)) + mix * float(analogue.get(k, 0.0))
            for k in keys
        }
    total = float(sum(max(0.0, v) for v in out.values()))
    if total <= 1e-12:
        return {k: 0.0 for k in keys}
    return {k: max(0.0, float(v)) / total for k, v in out.items()}


def build_weight_decomposition(
    *,
    w_base: Mapping[str, float],
    w_hist: Mapping[str, float],
    w_strategist: Mapping[str, float],
    w_past_analogue: Mapping[str, float] | None,
    w_final: Mapping[str, float],
    past_analogue_applied: bool,
) -> WeightDecomposition:
    """Build ΔW stack. Shadow: past_analogue delta uses shadow weights vs strategist."""
    notes: list[str] = []
    d_hist = _delta_maps(w_base, w_hist)
    d_strat = _delta_maps(w_hist, w_strategist)
    if w_past_analogue is not None:
        d_pa = _delta_maps(w_strategist, w_past_analogue)
        if not past_analogue_applied:
            notes.append("past_analogue shadow only — not applied to live weights")
        w_after_pa = dict(w_past_analogue) if past_analogue_applied else dict(w_strategist)
    else:
        d_pa = {k: 0.0 for k in w_strategist}
        w_after_pa = dict(w_strategist)
        notes.append("past_analogue ignored or unavailable")

    # Client pref + compliance folded into gap between post-strategist path and final
    # when we cannot split them: attribute residual to client_pref+compliance combined
    # as compliance for display when apply is off (final == strategist path).
    d_client = _delta_maps(w_after_pa, w_after_pa)  # zero placeholder
    d_comp = _delta_maps(w_after_pa, w_final)
    if _l1(d_comp) < 1e-9:
        notes.append("client_pref/compliance residual ≈ 0 vs strategist path")

    deltas = [
        WeightDeltaComponent(name="hist", l1=_l1(d_hist), per_ticker=d_hist),
        WeightDeltaComponent(name="strategist", l1=_l1(d_strat), per_ticker=d_strat),
        WeightDeltaComponent(name="past_analogue", l1=_l1(d_pa), per_ticker=d_pa),
        WeightDeltaComponent(name="client_pref", l1=_l1(d_client), per_ticker=d_client),
        WeightDeltaComponent(name="compliance", l1=_l1(d_comp), per_ticker=d_comp),
    ]

    # Identity: base + sum deltas ≈ final (applied path)
    recon: dict[str, float] = dict(w_base)
    for d in deltas:
        if d.name == "past_analogue" and not past_analogue_applied:
            continue
        for k, v in d.per_ticker.items():
            recon[k] = recon.get(k, 0.0) + v
    err = _l1(_delta_maps(recon, w_final))
    identity_ok = err < 1e-3
    if not identity_ok:
        notes.append(f"identity residual L1={err:.6f} (shadow past_analogue excluded)")

    return WeightDecomposition(
        w_base=dict(w_base),
        w_final=dict(w_final),
        deltas=deltas,
        identity_ok=identity_ok,
        notes=notes,
    )


def format_chat_summary(artifact: PastAnalogueArtifact) -> str:
    """One-line advisor-facing summary — detailed status stays in audit/messages."""
    if not artifact.enabled:
        return ""
    l1 = 0.0
    decomp = artifact.weight_decomposition
    if decomp:
        for d in decomp.deltas:
            if d.name == "past_analogue":
                l1 = float(d.l1)
                break
    return f"weight input from historical events = {l1:.3f}"


def run_past_analogue_pipeline(
    *,
    params: ScenarioParams,
    returns: pd.DataFrame,
    base_weights: Mapping[str, float],
    applied_weights: Mapping[str, float],
    strategy_view: StrategyView | Mapping[str, Any] | None,
    base_positions: Sequence[Mapping[str, Any]],
    trading_rules: Sequence[Mapping[str, Any]],
    portfolio_value: float | None,
    prices: Mapping[str, float] | None,
    ticker_currency: Mapping[str, str] | None,
    hkd_per_usd: float | None,
    client_alias: str | None,
    risk_aversion: float,
    freeze_list: Sequence[str],
    weight_overrides: Mapping[str, float],
    knobs: Mapping[str, Any],
    apply: bool = False,
) -> PastAnalogueArtifact:
    """Search → admit → shadow optimize → decomposition. Never raises to caller path."""
    messages: list[str] = []
    try:
        search = run_past_analogue_search(
            params,
            top_k=int(knobs.get("top_k") or DEFAULT_TOP_K),
            archive_years=int(knobs.get("archive_years") or 8),
            timeout_ms=int(knobs.get("search_timeout_ms") or 15_000),
            web_search=bool(knobs.get("web_search", True)),
        )
        messages.extend(list(search.get("messages") or []))
        raw_hits: list[PastAnaloguePeriodHit] = list(search.get("raw_hits") or [])

        annotated, admitted = admit_hits(
            raw_hits,
            returns,
            threshold=float(knobs.get("similarity_threshold") or DEFAULT_SIMILARITY_THRESHOLD),
            top_k=int(knobs.get("top_k") or DEFAULT_TOP_K),
            max_window_days=int(knobs.get("max_window_days") or DEFAULT_MAX_WINDOW_DAYS),
        )
        messages.append(
            f"past_analogue_admit: {len(admitted)}/{len(annotated)} hit(s) admitted"
        )

        weights_analogue: dict[str, float] = {}
        weights_shadow: dict[str, float] = {}
        positions_shadow: list[dict[str, Any]] = []
        analogue_mix = float(knobs.get("analogue_mix") or DEFAULT_ANALOGUE_MIX)
        if admitted:
            rw = reweight_returns(
                returns,
                admitted,
                trailing_blend=float(knobs.get("trailing_blend") or DEFAULT_TRAILING_BLEND),
            )
            # Same trading_rules / freeze / overrides as live opti — rules stay top priority.
            opti = propose_opti_folio(
                returns=rw,
                base_positions=base_positions,
                strategy_view=strategy_view,
                trading_rules=list(trading_rules),
                portfolio_value=portfolio_value,
                prices=prices,
                ticker_currency=ticker_currency,
                hkd_per_usd=hkd_per_usd,
                client_alias=client_alias,
                risk_aversion=float(risk_aversion or DEFAULT_RISK_AVERSION),
                freeze_list=list(freeze_list or []),
                weight_overrides=dict(weight_overrides or {}),
            )
            weights_analogue = {
                str(k): float(v) for k, v in (opti.get("weights") or {}).items()
            }
            # Soften aggression: blend strategist Opti Folio with analogue-conditioned Opti.
            weights_shadow = blend_strategist_analogue_weights(
                applied_weights,
                weights_analogue,
                analogue_mix=analogue_mix,
            )
            asset_classes = {
                str(p.get("ticker") or "").strip().upper(): _normalize_asset_class(
                    str(p.get("asset_class") or "EQUITY")
                )
                for p in base_positions
                if p.get("ticker")
            }
            for t in weights_shadow:
                asset_classes.setdefault(str(t).strip().upper(), "EQUITY")
            try:
                positions_shadow = weights_to_opti_positions(
                    weights_shadow,
                    asset_classes=asset_classes,
                    base_positions=base_positions,
                    portfolio_value=portfolio_value,
                    prices=prices,
                    ticker_currency=ticker_currency,
                    hkd_per_usd=hkd_per_usd,
                )
            except Exception as pos_exc:  # noqa: BLE001
                messages.append(f"past_analogue_positions_rebuild_failed: {pos_exc}")
                positions_shadow = [
                    p for p in (opti.get("positions") or []) if isinstance(p, dict)
                ]
            tag = "past_analogue_apply" if apply else "past_analogue_shadow"
            messages.append(
                f"{tag}: blended strategist {(1.0 - analogue_mix):.0%} + "
                f"analogue {analogue_mix:.0%} (pure-analogue L1 vs strategist="
                f"{_l1(_delta_maps(applied_weights, weights_analogue)):.3f})"
            )
            messages.extend(f"{tag}: {m}" for m in (opti.get("messages") or [])[:5])
        else:
            messages.append("past_analogue: skipped (no admitted hits)")

        # Ablation: hist = no strategist on trailing; strategist ≈ applied (pilot prefs bundled)
        w_hist: dict[str, float] = {}
        try:
            hist_opti = propose_opti_folio(
                returns=returns,
                base_positions=base_positions,
                strategy_view=None,
                trading_rules=list(trading_rules),
                portfolio_value=portfolio_value,
                prices=prices,
                ticker_currency=ticker_currency,
                hkd_per_usd=hkd_per_usd,
                client_alias=client_alias,
                risk_aversion=float(risk_aversion or DEFAULT_RISK_AVERSION),
                freeze_list=[],
                weight_overrides={},
            )
            w_hist = {
                str(k): float(v) for k, v in (hist_opti.get("weights") or {}).items()
            }
        except Exception as exc:  # noqa: BLE001
            messages.append(f"past_analogue_hist_ablation_failed: {exc}")
            w_hist = dict(applied_weights)

        # ΔW_past_analogue = blended applied path vs strategist (not pure analogue).
        decomp = build_weight_decomposition(
            w_base=base_weights,
            w_hist=w_hist,
            w_strategist=applied_weights,
            w_past_analogue=weights_shadow or None,
            w_final=applied_weights if not apply else (weights_shadow or applied_weights),
            past_analogue_applied=bool(apply and weights_shadow),
        )

        artifact = PastAnalogueArtifact(
            enabled=True,
            applied=bool(apply and weights_shadow),
            shadow=not bool(apply and weights_shadow),
            web_search_queries=list(search.get("web_search_queries") or []),
            search_snippets=list(search.get("search_snippets") or [])[:20],
            llm_raw_text=search.get("llm_raw_text"),
            llm_parse_attempts=int(search.get("llm_parse_attempts") or 0),
            raw_hits=annotated,
            admitted_hits=admitted,
            weights_shadow=weights_shadow,
            positions_shadow=positions_shadow,
            weight_decomposition=decomp,
            messages=messages,
        )
        artifact.chat_summary = format_chat_summary(artifact)
        return artifact
    except Exception as exc:  # noqa: BLE001
        logger.exception("past_analogue_pipeline_failed", error=str(exc))
        return PastAnalogueArtifact(
            enabled=True,
            applied=False,
            shadow=True,
            messages=[f"past_analogue_pipeline fail-open: {exc}"],
            chat_summary="weight input from historical events = 0.000",
        )
