# Pilot guide — Wealth Blotter Plus

Welcome. This guide walks you through **every step** from requesting access to using the Excel add-in. You do **not** need to be a developer — follow the steps in order, and ask your pilot host if you get stuck.

**What this pilot is**

- A private Excel add-in that talks to a hosted Wealth Blotter Plus server
- You register once in a web browser, wait for approval, then sign in from Excel
- You use **your own** LLM API key (bring your own key / BYOK). That key stays on your computer and is **not** stored on our servers

**What you will never receive (and should never ask for)**

- Database passwords, Supabase keys, Render passwords, or other pilots’ data

---

## Before you start — what the host must send you

You need all three:

| # | Item | Example |
|---|------|---------|
| 1 | **Request access link** (browser signup) | `https://wealth-blotter-plus.onrender.com/request-access` |
| 2 | **Invite to the private GitHub repository** | Email from GitHub |
| 3 | **API base URL** (for Excel config) | `https://wealth-blotter-plus.onrender.com` |

If any of these are missing, email the host and stop here.

**Also have ready**

- A Mac with **Microsoft Excel** (Microsoft 365 or a recent Excel for Mac)
- About 20–30 minutes for first-time setup
- Your **own** OpenAI or Anthropic API key (you already know how to get this)
- Permission to install small developer tools once (`Homebrew`, `mkcert`, `Node.js`) — ask IT if your company locks this down

---

## Checklist (print or keep open)

- [ ] Step A — Request access in the browser  
- [ ] Step B — Wait for “you’re approved” from the host  
- [ ] Step C — Accept GitHub invite and download the project  
- [ ] Step D — Install one-time Mac tools (Homebrew, Node, mkcert)  
- [ ] Step E — Point Excel at the hosted API (`.env` file)  
- [ ] Step F — Start the local task pane (`npm run dev`)  
- [ ] Step G — Load the add-in in Excel (sideload)  
- [ ] Step H — Paste your LLM key, sign in, use Copilot  

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

1. Wait for an email (or Slack/message) from the host saying you are approved.
2. Only then continue to Excel setup and sign-in.
3. If you try to sign in before approval, Excel will say your account is still pending — that is expected.

---

## Step C — Get the project from GitHub

### C1. Accept the GitHub invite

1. Open the invitation email from GitHub.
2. Click **Accept invitation** (you may need a free GitHub account with the same email the host invited).

### C2. Download the code (easiest: ZIP)

If you are not comfortable with `git`:

1. Open the repository page in your browser (the host’s private repo link).
2. Click the green **Code** button.
3. Click **Download ZIP**.
4. Unzip the file (double-click the ZIP).
5. You should get a folder named something like `wealth_blotter_plus-main`.  
   Rename it to `wealth_blotter_plus` if you like, and put it somewhere easy to find (for example your Documents folder).

### C3. Or clone with Terminal (optional)

If you already use git:

1. Open **Terminal** (Spotlight: press `Cmd + Space`, type `Terminal`, press Enter).
2. Copy and paste these lines (replace the URL with the one from GitHub → Code → HTTPS), then press Enter:

```bash
cd ~/Documents
git clone https://github.com/YOUR_ORG/wealth_blotter_plus.git
cd wealth_blotter_plus
```

---

## Step D — One-time Mac setup (tools)

Do this **once** on each Mac. You will use the **Terminal** app.

### D1. Install Homebrew (if you do not have it)

1. Open **Terminal**.
2. Paste this and press Enter:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

3. Follow the prompts. Enter your Mac login password when asked (characters will not show as you type — that is normal).
4. If Homebrew prints “Next steps” with two `echo` / `eval` lines, copy and run those too so `brew` works in new Terminal windows.

### D2. Install Node.js

```bash
brew install node
```

Check it worked:

```bash
node -v
npm -v
```

You should see version numbers (for example `v20…` and `10…`).

### D3. Install a trusted local HTTPS certificate (`mkcert`)

Excel requires a trusted certificate for the local task pane.

```bash
brew install mkcert
mkcert -install
```

Enter your Mac password if asked. If Excel later complains about certificates, **quit Excel completely** (`Cmd + Q`) and reopen it after this step.

---

## Step E — Point the add-in at the hosted API

You will create a tiny text file that tells Excel where the server is.

1. In Finder, open your project folder → open the **`frontend_excel`** folder.
2. Check whether a file named **`.env`** already exists.
   - On Mac, files starting with `.` are often hidden. In Finder: **View → Show View Options** or press `Cmd + Shift + .` to show hidden files.
3. If there is no `.env` file:
   - Open **TextEdit**
   - **Format → Make Plain Text**
   - Paste exactly one line (use the API URL the host gave you):

```bash
VITE_API_BASE_URL=https://wealth-blotter-plus.onrender.com
```

4. Save the file as **`.env`** inside the **`frontend_excel`** folder  
   (not in the project root, and not as `.env.txt`).
5. **Do not** put your LLM API key in this file.
6. **Do not** email this file with secrets or commit it to GitHub.

If the host’s URL is different, replace the part after `=` — **no space**, **no trailing slash**.

---

## Step F — Start the Excel task pane (every time you work)

Keep this Terminal window open while you use Excel.

1. Open **Terminal**.
2. Go to the Excel frontend folder (adjust the path if your folder is elsewhere):

```bash
cd ~/Documents/wealth_blotter_plus/frontend_excel
```

If you downloaded a ZIP named `wealth_blotter_plus-main`, use that folder name instead.

3. First time only in this folder — install packages (may take a few minutes):

```bash
npm install
npm run certs
```

4. Every session — start the local server:

```bash
npm run dev
```

5. Leave this running. You should see something mentioning `https://localhost:3000`.  
   Do **not** close Terminal until you are done working.

If you change the `.env` file later, stop the server (`Ctrl + C` in Terminal) and run `npm run dev` again.

---

## Step G — Load the add-in in Excel (sideload)

Do this while `npm run dev` is still running.

### G1. Open Excel

Open Microsoft Excel on your Mac (a blank workbook is fine).

### G2. Upload the manifest

Exact menus vary slightly by Excel version. Try this path first:

1. In Excel, go to **Insert** on the ribbon.
2. Click **Add-ins** (sometimes **My Add-ins** or **Get Add-ins**).
3. Open **My Add-ins** / **Developer Add-ins**.
4. Choose **Upload My Add-in** (or **Add from file**).
5. Browse to your project folder → **`frontend_excel`** → select **`manifest.xml`**.
6. Confirm / Upload.

Alternative (if you have the Developer tab):

1. **Excel → Preferences → Ribbon & Toolbar** → enable **Developer**.
2. **Developer → Add-ins → Excel Add-ins / My Add-ins → Upload**.

### G3. Open the Wealth Blotter Plus task pane

1. After the add-in loads, look for a **Wealth Blotter Plus** button on the ribbon (often under **Home** or a custom tab named in the manifest).
2. Click it to open the **task pane** on the right.
3. You should see fields for **Email**, **Password**, **LLM API key (BYOK)**, and **Provider**.

### If Excel says the certificate is not trusted

1. Quit Excel fully (`Cmd + Q`).
2. In Terminal, run again:

```bash
mkcert -install
cd ~/Documents/wealth_blotter_plus/frontend_excel
npm run certs
npm run dev
```

3. Reopen Excel and upload `manifest.xml` again.

---

## Step H — BYOK, sign in, and use the app

### H1. Bring your own LLM key (BYOK)

You already know how to create an API key with your provider. In the task pane:

1. Paste the key into **LLM API key (BYOK)**.
2. Choose **Provider**: OpenAI or Anthropic (match the key you created).
3. Keep this filled in for your session.  
   The key is sent only with your requests; it is **not** saved on the host’s server.

**Never**

- Put the key in the `.env` file  
- Commit the key to GitHub  
- Send the key to the host in email or chat  

### H2. Sign in

1. Enter the **same email** and **same password** you used on the request-access page.
2. Click **Sign in**.
3. The status line should show that you are signed in (not “pending” or an error).

If it says **pending approval**, stop and ask the host to approve your `profiles` row — do not register again.

### H3. Use Copilot / portfolio features

Once signed in:

1. Follow your host’s demo script (for example paste a small blotter grid and use **UPDATE PORTFOLIO** / Copilot chat).
2. Keep the Terminal `npm run dev` window running the whole time.
3. When finished: click **Sign out** in the pane, then stop the server in Terminal with `Ctrl + C`.

---

## Everyday use (after first-time setup)

Next time you work you only need:

1. Terminal:

```bash
cd ~/Documents/wealth_blotter_plus/frontend_excel
npm run dev
```

2. Open Excel → open the Wealth Blotter Plus task pane (re-upload `manifest.xml` only if the add-in disappeared).
3. Paste BYOK key → Sign in → work.

---

## Troubleshooting (plain language)

| What you see | What to try |
|--------------|-------------|
| “Registration failed” on the website | Tell the host; server env may be wrong. |
| Sign in says pending approval | Host has not approved you yet — wait. |
| Sign in fails after approval | Check email/password; confirm `.env` API URL; restart `npm run dev`. |
| Excel certificate / not trusted | Run `mkcert -install`, `npm run certs`, quit Excel (`Cmd + Q`), reopen. |
| Task pane blank or won’t load | Confirm Terminal still shows `npm run dev` running on `https://localhost:3000`. |
| LLM / Copilot errors about API key | Re-paste BYOK key; confirm Provider matches the key. |
| `command not found: brew` / `npm` | Finish Step D (Homebrew + Node). |
| Can’t find `.env` | In Finder press `Cmd + Shift + .` to show hidden files. |

---

## Privacy reminders

| Do | Don’t |
|----|--------|
| Use your own LLM API key | Ask the host for a shared LLM key |
| Keep your password and key private | Put secrets in GitHub or shared drives |
| Report bugs to the host | Share other pilots’ emails or client data |

Your blotter data is scoped to **your** advisor account. The host may see usage metrics tied to your account id — **not** your LLM key.

---

## Host / admin only

Deploy, env vars, and approving `profiles`: see [docs/PILOT_PHASE1.md](docs/PILOT_PHASE1.md).
