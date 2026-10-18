# Excel task pane

Office.js task pane (vanilla TypeScript + Vite).

**Pilot users:** follow **[PILOT.md](../PILOT.md)** — browser signup, one-time sideload of `manifest.pilot.xml`, then in Excel set **primary LLM** (API key and/or base URL) plus optional **secondary** key, and sign in. No Node or local server for the hosted pane.

**LLM fields (task pane)**

- **Primary (required for AI):** provider + API key and/or base URL (e.g. Ollama `http://127.0.0.1:11434`). Workhorse for agents / RAG.
- **Secondary (optional):** OpenAI, Anthropic, DeepSeek, Grok, Gemini, …. If blank, everything uses primary.
- Localhost Ollama only works when the API can reach that machine (local `npm run dev` / local uvicorn). Hosted Render cannot call the user’s laptop Ollama.

**Hosts:** deploy this folder as a Render Static Site — see [docs/PILOT_PHASE1.md](../docs/PILOT_PHASE1.md).

**Scenario / blotter notes**

- After optimize, `ord_blotter` shows **BUY** in bold green and **SELL** in bold red.
- Past Analogue (when enabled server-side) applies a blended historical tilt; chat shows
  `weight input from historical events = #.###`.
- Rating prompt after a run: “Rate the simulation below (optional)”.

---

## Production / pilot (hosted pane)

```bash
cd frontend_excel
npm ci
npm run build   # dist/ — VITE_API_BASE_URL from .env.production or env
```

- Pilots sideload **`manifest.pilot.xml`** (also published at `/manifest.pilot.xml` on the Static Site).
- Icons live under `public/assets/` and are copied into `dist/assets/`.

---

## Local development (HTTPS)

Excel blocks unsigned/self-signed certs (“not signed by a valid security certificate”).

### One-time setup

```bash
brew install mkcert
mkcert -install
cd frontend_excel
npm install
npm run certs
```

### Every session

```bash
cd frontend_excel
npm run dev
```

Sideload **`manifest.xml`** (localhost), not the pilot manifest.

If the error persists after `mkcert -install`, quit Excel fully (Cmd+Q) and reopen.

### Point at a remote API while developing locally

Create `frontend_excel/.env`:

```bash
VITE_API_BASE_URL=https://<service>.onrender.com
```

Restart `npm run dev` after changing this value.
