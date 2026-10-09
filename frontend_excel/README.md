# Excel task pane

Office.js task pane (vanilla TypeScript + Vite).

**Pilot users:** follow **[PILOT.md](../PILOT.md)** — browser signup, one-time sideload of `manifest.pilot.xml`, BYOK + sign-in in Excel. No Node or local server.

**Hosts:** deploy this folder as a Render Static Site — see [docs/PILOT_PHASE1.md](../docs/PILOT_PHASE1.md).

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
