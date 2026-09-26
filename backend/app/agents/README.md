# CrewAI + Ollama (local)

Local agent runs do **not** need a CrewAI cloud API key. Ollama serves the model on your machine.

## 1. Ollama (already installed on this Mac)

```bash
ollama serve          # if not already running
ollama list           # you have llama3.2 / llama3
# optional: ollama pull llama3.2
```

## 2. Configure `.env` (project root)

```bash
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2
LLM_TEMPERATURE=0.2
USE_CLOUD_LLM=false
```

| Variable | Purpose |
|----------|---------|
| `OLLAMA_BASE_URL` | Local Ollama HTTP API |
| `OLLAMA_MODEL` | Model tag from `ollama list` |
| `USE_CLOUD_LLM` | `false` = Ollama; `true` = OpenAI/Anthropic |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | Only if cloud mode |
| `CREWAI_API_KEY` | Optional CrewAI **cloud/enterprise** only — skip for local |

## 3. Agent definitions

- Roles: `backend/app/agents/config/agents.yaml`
- Tasks: `backend/app/agents/config/tasks.yaml`
- Orchestrator: `backend/app/agents/crew.py`
- LLM factory: `backend/app/agents/llm.py`
- Run audits: `backend/app/agents/audits/<workflow>/` (e.g. `portfolio_suggestions`)

Each advisory kickoff writes a sequenced markdown file under
`audits/portfolio_suggestions/local_audit_YYYYMMDDHHMM.md` listing every agent
output in order for manual review (no CrewAI Desktop required).

## 4. Try it

### Tiny hello-world crew (best first test)

```bash
# Ollama must be running (ollama list)
uv run python -m backend.app.agents.demo_hello
```

Files:
- Persona: `config/demo_hello_agents.yaml`
- Task: `config/demo_hello_tasks.yaml`
- Runner: `demo_hello.py`

### Advisory crew (later)

```bash
uv run uvicorn backend.app.main:app --reload --port 8000
# POST /api/v1/advisory/analyze
```

## 5. Research document RAG (POC)

Classification crew + Hugging Face embeddings. See
[backend/app/db/RAG_POC.md](../db/RAG_POC.md).

```bash
uv sync --extra rag
uv run uvicorn backend.app.main:app --reload --port 8000
# POST /api/v1/documents/upload
# Optional worker: uv run python -m backend.app.agents.rag_ingest_worker --once
```

| File | Role |
|------|------|
| `doc_classify.py` | Classification orchestrator |
| `config/doc_classify_*.yaml` | Persona + task |
| `tools/rag_retrieval_tool.py` | Filtered chunk lookup for analysis agents |
| `rag_ingest_worker.py` | Poll `document_ingest_jobs` |
