# Golden test cases

Drop ground-truth cases here as YAML lists. Pytest loads them via
`tests/golden/loader.py` — you usually edit YAML only, not Python.

| File | Covers |
|------|--------|
| `import_validation.yaml` | Normalize + `/portfolios/validate-import` |
| `compliance_guardrails.yaml` | Trading rules / max 25% single-name |
| `agent_drift.yaml` | Confidence routing + optional live LLM |
| `agent_routing.yaml` | House RAG wins, crypto sells, no large forbidden buys |

Each case needs `id`, `input`, and `expect`. Keep `client_ref` / aliases opaque (no PII).

```bash
uv run pytest tests/test_api_validation.py tests/test_compliance_guardrails.py \
  tests/test_agent_drift.py tests/test_agent_routing_golden.py -q

# Live LLM cases marked live_llm: true
WBP_LIVE_LLM=1 uv run pytest tests/test_agent_drift.py -q
```
