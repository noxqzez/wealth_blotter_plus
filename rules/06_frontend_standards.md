# Frontend Standards

- Locations: `frontend_excel/` (Vite + Office.js), `frontend_streamlit/` (ops dashboard)
- JS/TS: `camelCase` for functions and variables; strict null safety
- Separate Office.js API interaction layers from UI rendering logic
- Excel local HTTPS via `vite-plugin-mkcert` (`https://localhost:3000`)
- Never embed server secrets or service keys in the Excel task pane or Streamlit client
- Sanitize any user or LLM-generated content before DOM/render insertion (DOMPurify / bleach)
- Auth: Supabase Auth JWT in `Authorization: Bearer` only — no service role in browsers
