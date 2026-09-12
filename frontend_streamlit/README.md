# Streamlit ops dashboard

```bash
uv run streamlit run frontend_streamlit/app.py
```

Do not put service-role keys in Streamlit client secrets for browser exposure.
Prefer calling FastAPI with the user JWT, or server-side secrets only in `.streamlit/secrets.toml` (gitignored).
