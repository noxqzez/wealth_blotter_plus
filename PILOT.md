# Pilot guide — Wealth Blotter Plus

Welcome. This guide walks you from requesting access to using the Excel add-in. You do **not** need to install Node, certificates, or clone GitHub.

**What this pilot is**

- A private Excel add-in whose task pane is hosted in the cloud
- You register once in a web browser, wait for approval, then sign in from Excel
- You configure a **primary LLM** (API key and/or local base URL such as Ollama) and an optional **secondary** cloud key. Credentials stay on your computer and are **not** stored on our servers
- **Corporate SSO is not available for this pilot yet.** Use email/password only.

---

## Before you start — what the host must send you

You need:

| # | Item | Example |
|---|------|---------|
| 1 | **Request access link** (browser signup) | `https://wealth-blotter-plus.onrender.com/request-access` |
| 2 | **Add-in manifest** (download link or file) | `https://wealth-blotter-plus-excel.onrender.com/manifest.pilot.xml` |

If either is missing, email the host and stop here.

**Also have ready**

- A computer with **desktop Microsoft Excel** — **Mac or Windows** both work  
  (Microsoft 365 desktop Excel, or a recent Excel for Mac / Excel for Windows).  
  Excel **in a web browser only** (Excel Online) is not supported for this pilot.
- About 10 minutes for first-time setup
- A **primary LLM**: API key (OpenAI, Anthropic, DeepSeek, Grok, Gemini, …) **and/or** a reachable base URL (e.g. local Ollama). Optional secondary key if you have another provider

---

## Checklist

- [ ] Step A — Request access in the browser  
- [ ] Step B — Wait for “you’re approved” from the host  
- [ ] Step C — Download the add-in manifest (once)  
- [ ] Step D — Load the add-in in Excel (once per computer)  
- [ ] Step E — Configure primary (+ optional secondary) LLM, sign in, use Copilot  

---

## Step A — Request access (web browser)

1. Open the **request access link** the host sent you  
   (example: [https://wealth-blotter-plus.onrender.com/request-access](https://wealth-blotter-plus.onrender.com/request-access)).
2. Enter your **work email**.
3. Choose a **password** (at least 8 characters). Write it down somewhere safe — you will use the **same** password later in Excel.
4. Confirm the password.
5. Click **Request access**.
6. You should see a green success message that registration was received and an admin must approve you.

**Important**

- You **cannot** use the product until the host approves you.
- Do **not** create a second account if login later says “pending” — wait for approval.

---

## Step B — Wait for approval

1. Wait for a **welcome email** from the host (after they approve you). It includes the manifest download link and setup steps.  
   (Some hosts may still message you separately in Slack.)
2. Only then continue to Excel setup and sign-in.
3. If you try to sign in before approval, Excel will say your account is still pending — that is expected.

---

## Step C — Download the add-in manifest (once)

1. Open the **manifest download link** the host sent you  
   (example: [https://wealth-blotter-plus-excel.onrender.com/manifest.pilot.xml](https://wealth-blotter-plus-excel.onrender.com/manifest.pilot.xml)).
2. Save the file as **`manifest.pilot.xml`** somewhere easy to find (Downloads is fine).  
   Do not rename it to `.txt`.

You do **not** need GitHub, Terminal, Node.js, or `npm`.

---

## Step D — Load the add-in in Excel (once per computer)

### D1. Open Excel

Open **desktop** Microsoft Excel (a blank workbook is fine).

### D2. Upload the manifest

Exact menus vary slightly by Excel version. Try this path first:

1. In Excel, go to **Insert** on the ribbon.
2. Click **Add-ins** (sometimes **My Add-ins** or **Get Add-ins**).
3. Open **My Add-ins** / **Developer Add-ins**.
4. Choose **Upload My Add-in** (or **Add from file**).
5. Select the **`manifest.pilot.xml`** file you downloaded.
6. Confirm / Upload.

**Mac alternative** (Developer tab):

1. **Excel → Preferences → Ribbon & Toolbar** → enable **Developer**.
2. **Developer → Add-ins → Upload**.

**Windows alternative** (shared folder sideload, if Upload is missing):

1. Create a folder, e.g. `C:\Users\YourName\ExcelAddins`.
2. Copy `manifest.pilot.xml` into that folder.
3. In Excel: **File → Options → Trust Center → Trust Center Settings → Trusted Add-in Catalogs**.
4. Add the folder path, check **Show in Menu**, restart Excel.
5. **Insert → My Add-ins → Shared Folder** → select the add-in.

### D3. Open the task pane

1. After the add-in loads, look for a **Copilot** / Wealth Blotter button on the ribbon.
2. Click it to open the **task pane** on the right.
3. You should see **Email**, **Password**, **Primary LLM** (provider, API key, base URL), and **Secondary LLM (optional)**.

The task pane loads from the host’s cloud site. You do **not** run a local server for the hosted pilot.

If the pane is blank, check your network/VPN and ask the host to confirm the hosted task pane is online.

**Mac tip:** if Insert has no Upload My Add-in, copy `manifest.pilot.xml` into  
`~/Library/Containers/com.microsoft.Excel/Data/Documents/wef/`, Quit Excel, reopen.  
Do **not** use Developer → Excel Add-ins (that dialog is only for Analysis ToolPak / Solver).

---

## Step E — LLM setup, sign in, and use the app

### E1. Primary + optional secondary LLM

In the Excel task pane:

**Primary LLM (required for AI features)**

1. Choose **Primary provider** (Ollama, OpenAI, Anthropic, DeepSeek, Grok, Gemini, …).
2. Enter a **Primary API key** and/or a **Primary base URL**.  
   - Example local Ollama: provider `Ollama`, base URL `http://127.0.0.1:11434` (key can be blank).  
   - Example cloud only: provider `DeepSeek` (or OpenAI / …) + API key.
3. Primary is the **workhorse** for agents (including in-house research / RAG).

**Secondary LLM (optional)**

1. Choose a secondary provider and paste its API key if you have one.
2. If secondary is blank, **everything uses primary**.

Keys are sent only with your requests; they are **not** saved on the host’s server.

**Important (hosted vs local):** a base URL of `http://127.0.0.1:11434` only works when the **API process can reach that machine** (typical for local developer setup). A cloud-hosted API **cannot** call Ollama on your laptop.

**Never**

- Put keys in email or chat to the host  
- Commit keys to GitHub  
- Paste keys on the request-access web page (LLM fields are **only** in the Excel pane)

### E2. Sign in

1. Enter the **same email** and **same password** you used on the request-access page.
2. Click **Sign in** (primary LLM must be filled in: key and/or base URL).
3. The status line should show that you are signed in (not “pending” or an error).

If it says **pending approval**, stop and ask the host to approve your account — do not register again.

If it says **Session expired — sign in again**, enter your password again (and keep primary / secondary LLM fields filled in).

### E3. Use Copilot / portfolio features

Once signed in:

1. Follow your host’s demo script (for example paste a small blotter grid and use Copilot chat).
2. When finished: click **Sign out** in the pane.

---

## Everyday use (after first-time setup)

Next time you work you only need:

1. Open Excel.
2. Open the Copilot / Wealth Blotter task pane (re-upload `manifest.pilot.xml` only if the add-in disappeared).
3. Confirm primary LLM (and optional secondary) → Sign in → work.

---

## Troubleshooting (plain language)

| What you see | What to try |
|--------------|-------------|
| “Registration failed” on the website | Tell the host; server env may be wrong. |
| Sign in says pending approval | Host has not approved you yet — wait. |
| Sign in fails after approval | Check email/password; ask host if the API is up. |
| Task pane blank or won’t load | Confirm you used `manifest.pilot.xml` (hosted), not an old localhost manifest; ask host if the Static Site is deployed. |
| “Session expired — sign in again” | Sign in again with the same email/password; re-check primary LLM fields. |
| LLM / Copilot errors about API key / primary | Enter primary API key and/or base URL; provider must match. Secondary is optional. |
| Ollama / localhost not working on hosted pilot | Expected if the API is on Render — use a cloud primary key, or run the API locally. |
| Can’t find Upload My Add-in | Mac: use the `wef` folder tip above; Windows: Trusted Add-in Catalogs. Don’t use Excel Add-ins (ToolPak). |

---

## Privacy reminders

| Do | Don’t |
|----|--------|
| Use your own primary (and optional secondary) LLM credentials | Ask the host for a shared LLM key |
| Keep your password and key private | Put secrets in email or shared drives |
| Report bugs to the host | Share other pilots’ emails or client data |

Your blotter data is scoped to **your** advisor account. The host may see usage metrics tied to your account id — **not** your LLM key.

---

## Host / admin only

Deploy, env vars, Static Site, CORS, and approving `profiles`: see [docs/PILOT_PHASE1.md](docs/PILOT_PHASE1.md).
