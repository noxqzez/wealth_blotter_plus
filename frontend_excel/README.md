# Excel task pane — local HTTPS (Mac)

Excel blocks unsigned/self-signed certs (“not signed by a valid security certificate”).

**Pilot users:** full register → approval → BYOK → sign-in steps are in the repo root **[PILOT.md](../PILOT.md)**.

## One-time setup (run in Terminal.app — needs your Mac password)

```bash
brew install mkcert
mkcert -install
cd frontend_excel
npm install
npm run certs
```

## Every session

```bash
cd frontend_excel
npm run dev
```

Then sideload `manifest.xml` in Excel.

If the error persists after `mkcert -install`, quit Excel fully (Cmd+Q) and reopen.

## Point at the hosted API (pilots)

Create `frontend_excel/.env`:

```bash
VITE_API_BASE_URL=https://<service>.onrender.com
```

Restart `npm run dev` after changing this value.
