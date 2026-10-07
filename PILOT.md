# Pilot guide — Wealth Blotter Plus

You are invited to a **closed pilot**. This page is everything you need to request access and run the Excel add-in against the hosted API.

You will **not** receive database secrets, Render credentials, or other advisors’ data. Your LLM API key stays in your browser and is never stored on our servers.

---

## What you need from the host

Before you start, the pilot host should give you:

1. The **request access URL** (share this freely with invited pilots):  
   `https://<service>.onrender.com/request-access`  
   (the bare API host `https://<service>.onrender.com` redirects here too)
2. Access to this **private GitHub repository** (for the Excel add-in)
3. The **API base URL** for Excel config: `https://<service>.onrender.com`

If you do not have the request-access URL and repo access, stop and ask the host.

---

## Step 1 — Request access (browser)

1. Open `https://<service>.onrender.com/request-access`
2. Enter your **work email** and a **password** (at least 8 characters)
3. Click **Request access**

You should see confirmation that registration was received and you are **awaiting approval**.

You cannot use Copilot until the host approves you and you sign in from Excel.

*(You can also register from the Excel task pane later; the browser page is the easy shareable path.)*

---

## Step 2 — Wait for approval

The host will approve your account in Supabase (`profiles.approval_status = APPROVED`) and should email you when you are cleared.

If Sign in returns that your account is pending, wait for that email—do not create a second account unless the host asks you to.

---

## Step 3 — Clone the repo

```bash
git clone <repo-url-from-host>
cd wealth_blotter_plus
```

Use the URL from your GitHub invite email.

---

## Step 4 — Point the Excel add-in at the hosted API

Create or edit `frontend_excel/.env`:

```bash
VITE_API_BASE_URL=https://<service>.onrender.com
```

Replace with the exact URL the host sent you (no trailing slash).

Do **not** put your OpenAI/Anthropic key in this file or in any committed file.

---

## Step 5 — Install and run the Excel task pane (local HTTPS)

One-time (Mac; needs your password for the local cert):

```bash
brew install mkcert
mkcert -install
cd frontend_excel
npm install
npm run certs
```

Every session:

```bash
cd frontend_excel
npm run dev
```

Sideload `frontend_excel/manifest.xml` in Excel, then open the Wealth Blotter Plus task pane.

More detail: [frontend_excel/README.md](frontend_excel/README.md).

---

## Step 6 — Add your own LLM key (BYOK)

1. Get an API key from **your** OpenAI or Anthropic account
2. In the task pane, paste it into **LLM API key (BYOK)**
3. Keep that field filled for the session (it is sent only as a request header; it is not saved on the server)

**Never** commit the key, put it in `.env` that you push, or share it in chat/email with the host.

---

## Step 7 — Sign in and use Copilot

1. In the Excel task pane, enter the **same email/password** you used on the request-access page
2. Click **Sign in**
3. Confirm the panel shows you as signed in
4. Use Copilot / portfolio flows as usual

If Sign in fails after approval, double-check `VITE_API_BASE_URL`, restart `npm run dev`, and confirm the BYOK field is filled when calling LLM routes.

---

## Privacy & security (please follow)

| Do | Don’t |
|----|--------|
| Use your own LLM API key | Ask the host for a shared LLM key |
| Keep passwords and keys private | Commit `.env` files with secrets |
| Report bugs to the host | Share other pilots’ emails or data |

Your portfolio and client data are scoped to **your** advisor account. Usage metrics the host sees are tied to your advisor id only—not your LLM key.

---

## Quick checklist

- [ ] Open **/request-access** → register → wait for approval email  
- [ ] Repo access + API URL from host  
- [ ] `frontend_excel/.env` → `VITE_API_BASE_URL=…`  
- [ ] `npm run certs` + `npm run dev` + sideload manifest  
- [ ] Paste **your** LLM key (BYOK)  
- [ ] **Sign in** in Excel → use Copilot  

---

## Host / admin setup

Operators deploying Render and approving accounts: see [docs/PILOT_PHASE1.md](docs/PILOT_PHASE1.md).
