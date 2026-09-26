"""Cite audit events against trading_rules and versioned configs."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from backend.app.core.supabase import get_service_supabase
from backend.app.schemas.compliance import (
    AuditEventRead,
    ComplianceCitation,
)
from backend.app.schemas.rules import TechnicalLimits
from backend.app.services.trading_rules_policy import (
    max_single_name_weight_from_rules,
)

logger = structlog.get_logger(__name__)


async def load_active_trading_rules() -> list[dict[str, Any]]:
    client = get_service_supabase()
    result = (
        client.table("trading_rules")
        .select("id,rule_name,rule_category,min_value,max_value,parameters,is_active")
        .eq("is_active", True)
        .execute()
    )
    return list(result.data or [])


async def load_configs_with_versions() -> list[dict[str, Any]]:
    client = get_service_supabase()
    result = client.table("configs").select("key,config,version,updated_at").execute()
    return list(result.data or [])


def _payload_weights(event: AuditEventRead) -> list[float]:
    weights: list[float] = []
    positions = event.payload.get("positions")
    if isinstance(positions, list):
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            w = pos.get("current_weight_pct")
            if isinstance(w, (int, float)):
                weights.append(float(w))
    return weights


def cite_events(
    events: list[AuditEventRead],
    *,
    rules: list[dict[str, Any]],
    configs: list[dict[str, Any]],
) -> list[ComplianceCitation]:
    """
    Deterministic cite step: attach matching trading_rules / configs to events.

    Evaluates hard thresholds in code (no LLM). Citations always include the
    audit_event_id when an event is in scope.
    """
    citations: list[ComplianceCitation] = []
    config_by_key = {c["key"]: c for c in configs if "key" in c}

    tech = _parse_tech(config_by_key.get("technical_limits"))
    max_single = max_single_name_weight_from_rules(rules)

    for event in events:
        citations.append(
            ComplianceCitation(
                audit_event_id=event.id,
                source_type=event.source_type,
                source_name=event.source_name,
                note=f"Audit log {event.id} ({event.event_action})",
            )
        )

        # Config citations — technical ops + soft product knobs only
        if "technical_limits" in config_by_key:
            row = config_by_key["technical_limits"]
            citations.append(
                ComplianceCitation(
                    audit_event_id=event.id,
                    source_type=event.source_type,
                    source_name=event.source_name,
                    config_key="technical_limits",
                    config_version=int(row.get("version", 1)),
                    note="Technical limits (rules-as-code)",
                )
            )
        if "business_guardrails" in config_by_key:
            row = config_by_key["business_guardrails"]
            citations.append(
                ComplianceCitation(
                    audit_event_id=event.id,
                    source_type=event.source_type,
                    source_name=event.source_name,
                    config_key="business_guardrails",
                    config_version=int(row.get("version", 1)),
                    note="Business guardrails (coin_temperature / confidence)",
                )
            )

        weights = _payload_weights(event)
        max_w = max(weights) if weights else None
        row_count = event.payload.get("row_count") or event.normalized_params.get("row_count")
        ticker_count = event.payload.get("ticker_count") or event.normalized_params.get(
            "ticker_count"
        )

        for rule in rules:
            rule_id = UUID(str(rule["id"])) if rule.get("id") else None
            name = str(rule.get("rule_name", ""))
            category = str(rule.get("rule_category", ""))
            max_value = rule.get("max_value")
            note: str | None = None

            if (
                max_w is not None
                and max_value is not None
                and "single" in name.lower()
                and max_w > float(max_value)
            ):
                note = (
                    f"Position weight {max_w:.4f} exceeds rule max {float(max_value):.4f} "
                    f"({name})"
                )
            elif category.lower() in {
                "concentration",
                "weight",
                "portfolio",
                "position_limit",
            } and max_w is not None:
                note = f"Reviewed against {name} (observed max weight {max_w:.4f})"
            elif event.event_action in {"portfolio_update", "portfolio_flagged"}:
                note = f"Policy context: {name}"

            if note and rule_id is not None:
                citations.append(
                    ComplianceCitation(
                        audit_event_id=event.id,
                        source_type=event.source_type,
                        source_name=event.source_name,
                        rule_id=rule_id,
                        rule_name=name,
                        note=note,
                    )
                )

        # Single-name cap from trading_rules (not business_guardrails)
        if max_single is not None and max_w is not None and max_w > max_single:
            single_rule = next(
                (
                    r
                    for r in rules
                    if str(r.get("rule_name", "")).lower() == "max_single_name_weight"
                ),
                None,
            )
            citations.append(
                ComplianceCitation(
                    audit_event_id=event.id,
                    source_type=event.source_type,
                    source_name=event.source_name,
                    rule_id=(
                        UUID(str(single_rule["id"]))
                        if single_rule and single_rule.get("id")
                        else None
                    ),
                    rule_name="max_single_name_weight",
                    note=(
                        f"Max single-name weight {max_w:.4f} > "
                        f"max_single_name_weight {max_single:.4f}"
                    ),
                )
            )

        if (
            tech
            and isinstance(row_count, (int, float))
            and float(row_count) > tech.max_batch_ingest_rows
        ):
            row = config_by_key.get("technical_limits", {})
            citations.append(
                ComplianceCitation(
                    audit_event_id=event.id,
                    source_type=event.source_type,
                    source_name=event.source_name,
                    config_key="technical_limits",
                    config_version=int(row.get("version", 1)),
                    note=(
                        f"Row count {int(row_count)} exceeds technical_limits "
                        f"max_batch_ingest_rows {tech.max_batch_ingest_rows}"
                    ),
                )
            )

        if (
            tech
            and isinstance(ticker_count, (int, float))
            and float(ticker_count) > tech.max_tickers_per_request
        ):
            row = config_by_key.get("technical_limits", {})
            citations.append(
                ComplianceCitation(
                    audit_event_id=event.id,
                    source_type=event.source_type,
                    source_name=event.source_name,
                    config_key="technical_limits",
                    config_version=int(row.get("version", 1)),
                    note=(
                        f"Ticker count {int(ticker_count)} > "
                        f"max_tickers_per_request {tech.max_tickers_per_request}"
                    ),
                )
            )

    # Deduplicate by (audit_event_id, rule_id, config_key, note)
    seen: set[tuple[Any, ...]] = set()
    unique: list[ComplianceCitation] = []
    for c in citations:
        key = (c.audit_event_id, c.rule_id, c.config_key, c.note)
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)

    logger.info("citations_built", events=len(events), citations=len(unique))
    return unique


def _parse_tech(row: dict[str, Any] | None) -> TechnicalLimits | None:
    if not row:
        return None
    cfg = row.get("config")
    if not isinstance(cfg, dict):
        return None
    return TechnicalLimits.model_validate(cfg)


async def cite_audit_events(events: list[AuditEventRead]) -> list[ComplianceCitation]:
    rules = await load_active_trading_rules()
    configs = await load_configs_with_versions()
    return cite_events(events, rules=rules, configs=configs)
