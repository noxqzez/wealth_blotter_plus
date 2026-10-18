"""Past Analogue worker: web search + BYOK LLM JSON extract.

Fail-open: missing search key / LLM errors → empty hits + messages.
Never emits portfolio weights. Anthropic web-search tool is not used.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx
import structlog

from backend.app.config import get_settings
from backend.app.schemas.scenario import PastAnaloguePeriodHit, ScenarioParams

logger = structlog.get_logger(__name__)

# Process-lifetime live Tavily HTTP call counter (pilot credit guard).
_tavily_live_calls = 0


def _tavily_cache_path(cache_dir: str, query: str) -> Path:
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:24]
    return Path(cache_dir) / f"{digest}.json"


def _load_tavily_cache(cache_dir: str, query: str) -> list[dict[str, Any]] | None:
    path = _tavily_cache_path(cache_dir, query)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("snippets") if isinstance(data, dict) else None
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _save_tavily_cache(cache_dir: str, query: str, snippets: list[dict[str, Any]]) -> None:
    path = _tavily_cache_path(cache_dir, query)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"query": query, "snippets": snippets}, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("tavily_cache_write_failed", error=str(exc), path=str(path))

_EXTRACT_PROMPT = """You extract historical market analogue periods for portfolio research.
Given the scenario and web search snippets, return ONLY a JSON object:
{{
  "hits": [
    {{
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD",
      "label": "short name",
      "rationale": "why this matches",
      "model_similarity": 0.0,
      "source_urls": ["https://..."]
    }}
  ]
}}
Rules:
- model_similarity in [0,1]; prefer fewer high-quality hits (max {top_k}).
- Prefer periods inside the last {archive_years} years when possible.
- Use URLs from the snippets when available; do not invent portfolio weights.
- If nothing matches, return {{"hits": []}}.

Scenario prompt: {scenario_prompt}
Scenario type: {scenario_type}
Tone: {tone}
Tags: {tags}

Web search queries: {queries}

Snippets:
{snippets}
"""


def _build_queries(params: ScenarioParams, *, max_queries: int = 1) -> list[str]:
    prompt = (params.user_prompt or "").strip()
    stype = params.scenario.scenario_type or "market_scenario"
    tags = ", ".join(params.research_context_tags[:8] or params.detected_factors.macro_factors[:8])
    q1 = f"historical periods similar to: {prompt[:200]}"
    q2 = f"{stype} market reaction historical dates {tags}".strip()
    all_q = [q1, q2]
    n = max(1, min(int(max_queries or 1), len(all_q)))
    return all_q[:n]


def _search_tavily(query: str, *, api_key: str, timeout_s: float) -> list[dict[str, Any]]:
    with httpx.Client(timeout=timeout_s) as client:
        resp = client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": 5,
                "include_answer": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    out: list[dict[str, Any]] = []
    for row in data.get("results") or []:
        out.append(
            {
                "title": str(row.get("title") or ""),
                "url": str(row.get("url") or ""),
                "content": str(row.get("content") or "")[:800],
                "query": query,
            }
        )
    return out


def _search_duckduckgo_html(query: str, *, timeout_s: float) -> list[dict[str, Any]]:
    """Best-effort public fallback (no key). Results may be thin; audit stores raw."""
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
        resp = client.get(url, headers={"User-Agent": "WealthBlotterPlus/1.0"})
        resp.raise_for_status()
        html = resp.text
    # Crude link extraction; enough for LLM context + source URLs.
    links = re.findall(
        r'uddg=([^&"]+).*?class="result__a"[^>]*>([^<]+)',
        html,
        flags=re.I | re.S,
    )
    out: list[dict[str, Any]] = []
    for href, title in links[:5]:
        from urllib.parse import unquote

        out.append(
            {
                "title": title.strip(),
                "url": unquote(href)[:500],
                "content": title.strip(),
                "query": query,
            }
        )
    return out


def run_web_search(
    params: ScenarioParams,
    *,
    timeout_ms: int = 15_000,
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """Return (queries, snippets, messages). Never raises.

    Pilot thrift: disk-cache Tavily by query; respect ``TAVILY_MAX_CALLS_PER_PROCESS``.
    """
    global _tavily_live_calls
    messages: list[str] = []
    settings = get_settings()
    max_q = int(getattr(settings, "past_analogue_max_search_queries", 1) or 1)
    queries = _build_queries(params, max_queries=max_q)
    snippets: list[dict[str, Any]] = []
    timeout_s = max(3.0, float(timeout_ms) / 1000.0)
    tavily = (getattr(settings, "tavily_api_key", None) or "").strip()
    if not tavily:
        import os

        tavily = (os.environ.get("TAVILY_API_KEY") or "").strip()
    cache_dir = (getattr(settings, "tavily_cache_dir", None) or "").strip() or (
        "artifacts/past_analogue/tavily_cache"
    )
    max_calls = int(getattr(settings, "tavily_max_calls_per_process", 40) or 0)

    for q in queries:
        try:
            if tavily:
                cached = _load_tavily_cache(cache_dir, q)
                if cached is not None:
                    snippets.extend(cached)
                    messages.append(
                        f"past_analogue_search: tavily CACHE hit (0 credits) qlen={len(q)}"
                    )
                    continue
                if max_calls > 0 and _tavily_live_calls >= max_calls:
                    messages.append(
                        f"past_analogue_search: Tavily live-call cap reached "
                        f"({_tavily_live_calls}/{max_calls}) — skipping remaining queries"
                    )
                    break
                rows = _search_tavily(q, api_key=tavily, timeout_s=timeout_s)
                _tavily_live_calls += 1
                _save_tavily_cache(cache_dir, q, rows)
                snippets.extend(rows)
                messages.append(
                    f"past_analogue_search: tavily LIVE call #{_tavily_live_calls} "
                    f"qlen={len(q)} (cached for reruns)"
                )
            else:
                snippets.extend(_search_duckduckgo_html(q, timeout_s=timeout_s))
                messages.append(
                    "past_analogue_search: TAVILY_API_KEY unset — used public fallback"
                )
        except Exception as exc:  # noqa: BLE001
            messages.append(f"past_analogue_search: query failed: {exc}")
            logger.warning("past_analogue_search_failed", error=str(exc), query=q[:120])

    return queries, snippets, messages


def _strip_fences(text: str) -> str:
    content = text.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    return content.strip()


def extract_periods_with_llm(
    *,
    params: ScenarioParams,
    queries: list[str],
    snippets: list[dict[str, Any]],
    top_k: int,
    archive_years: int,
    max_attempts: int = 3,
) -> tuple[list[PastAnaloguePeriodHit], str | None, int, list[str]]:
    """BYOK/Ollama JSON extract. Fail-open to [].

    ``PAST_ANALOGUE_LLM_PROVIDER=gemini`` is recognized as a stub only —
    Gemini is not wired in ``build_llm`` yet; extract still uses the default LLM.
    """
    messages: list[str] = []
    settings = get_settings()
    override = (getattr(settings, "past_analogue_llm_provider", None) or "").strip().lower()
    if override == "gemini":
        messages.append(
            "past_analogue_llm: PAST_ANALOGUE_LLM_PROVIDER=gemini is a placeholder — "
            "using default build_llm() until Gemini is wired"
        )
        if not (getattr(settings, "gemini_api_key", None) or "").strip():
            messages.append("past_analogue_llm: GEMINI_API_KEY is empty (stub)")
    snippet_blob = "\n\n".join(
        f"- {s.get('title')}\n  URL: {s.get('url')}\n  {s.get('content')}"
        for s in snippets[:12]
    ) or "(no snippets)"
    prompt = _EXTRACT_PROMPT.format(
        top_k=top_k,
        archive_years=archive_years,
        scenario_prompt=(params.user_prompt or "")[:1500],
        scenario_type=params.scenario.scenario_type,
        tone=params.scenario.tone,
        tags=", ".join(params.research_context_tags[:12]),
        queries=json.dumps(queries),
        snippets=snippet_blob[:6000],
    )

    raw_text: str | None = None
    attempts = 0
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            from backend.app.agents.llm import build_llm

            llm = build_llm(temperature=0.2)
            heal = prompt
            if last_err:
                heal = (
                    prompt
                    + f"\n\nPrevious output failed validation: {last_err}\n"
                    "Return corrected JSON only."
                )
            raw: Any = llm.call(heal)
            raw_text = raw if isinstance(raw, str) else str(raw)
            data = json.loads(_strip_fences(raw_text))
            hits_raw = data.get("hits") if isinstance(data, dict) else None
            if not isinstance(hits_raw, list):
                raise ValueError("missing hits array")
            hits: list[PastAnaloguePeriodHit] = []
            for row in hits_raw[:top_k]:
                if not isinstance(row, dict):
                    continue
                hits.append(
                    PastAnaloguePeriodHit(
                        start_date=str(row.get("start_date") or "")[:32],
                        end_date=str(row.get("end_date") or "")[:32],
                        label=str(row.get("label") or "")[:200],
                        rationale=str(row.get("rationale") or "")[:1000],
                        model_similarity=float(row.get("model_similarity") or 0.0),
                        source_urls=[
                            str(u) for u in (row.get("source_urls") or []) if u
                        ][:8],
                        web_search_queries=list(queries),
                    )
                )
            messages.append(f"past_analogue_llm: parsed {len(hits)} hit(s) attempt={attempt}")
            return hits, raw_text, attempts, messages
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            messages.append(f"past_analogue_llm: attempt {attempt} failed: {exc}")
            logger.warning("past_analogue_llm_extract_failed", attempt=attempt, error=str(exc))

    return [], raw_text, attempts, messages


def run_past_analogue_search(
    params: ScenarioParams,
    *,
    top_k: int = 5,
    archive_years: int = 8,
    timeout_ms: int = 15_000,
    web_search: bool = True,
) -> dict[str, Any]:
    """Full worker: search → LLM extract. Always returns a dict (fail-open)."""
    messages: list[str] = []
    queries: list[str] = []
    snippets: list[dict[str, Any]] = []
    if web_search:
        queries, snippets, sm = run_web_search(params, timeout_ms=timeout_ms)
        messages.extend(sm)
    else:
        messages.append("past_analogue_search: web_search disabled by flag")
        queries = _build_queries(params)

    hits, raw_text, attempts, lm = extract_periods_with_llm(
        params=params,
        queries=queries,
        snippets=snippets,
        top_k=top_k,
        archive_years=archive_years,
    )
    messages.extend(lm)
    return {
        "web_search_queries": queries,
        "search_snippets": snippets,
        "llm_raw_text": raw_text,
        "llm_parse_attempts": attempts,
        "raw_hits": hits,
        "messages": messages,
    }
