"""Pilot welcome email (Resend) — manifest link + setup steps. No passwords."""

from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx
import structlog

from backend.app.config import Settings, get_settings

logger = structlog.get_logger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"

STARTER_CSV = (
    "client_alias,ticker,quantity,asset_class,currency\n"
    "demo_client,AAPL,100,EQUITY,USD\n"
    "demo_client,MSFT,50,EQUITY,USD\n"
    "demo_client,CASH_USD,10000,CASH,USD\n"
)


class WelcomeEmailError(Exception):
    """Resend or config failure when sending the pilot welcome email."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class WelcomeEmailResult:
    provider_id: str | None
    to_email: str


def default_manifest_url(settings: Settings | None = None) -> str:
    """Return optional LIVE manifest URL, or empty to use email attachment."""
    cfg = settings or get_settings()
    explicit = (cfg.pilot_manifest_url or "").strip()
    if explicit:
        return explicit.rstrip("/")
    # Do not invent Static Site …/manifest.pilot.xml — that file may be placeholders.
    return ""


def _manifest_step_text(manifest_url: str) -> str:
    url = (manifest_url or "").strip()
    if url:
        return (
            "1) Download the Excel add-in manifest\n"
            f"   {url}\n"
            "   Save it as manifest.pilot.xml (do not rename to .txt).\n"
            "   Tip: if the browser shows XML instead of downloading, use File → Save As,\n"
            f'   or on Mac: curl -L -o ~/Downloads/manifest.pilot.xml "{url}"\n'
        )
    return (
        "1) Use the Excel add-in manifest attached to this email "
        "(or separately from your host)\n"
        "   Save it as manifest.pilot.xml (do not rename to .txt).\n"
        "   Do not use placeholder XML from GitHub or from …/manifest.pilot.xml "
        "on the public Static Site.\n"
    )


def _manifest_step_html(manifest_url: str) -> str:
    url = (manifest_url or "").strip()
    if url:
        safe_url = (
            url.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
        )
        return (
            "<strong>Download the add-in manifest</strong><br/>"
            f'<a href="{safe_url}">{safe_url}</a><br/>'
            "Save as <code>manifest.pilot.xml</code> (do not rename to .txt). "
            "If the browser only shows XML, use <em>File → Save As</em>."
        )
    return (
        "<strong>Use the add-in manifest from your host</strong> "
        "(email attachment of the LIVE file)<br/>"
        "Save as <code>manifest.pilot.xml</code>. "
        "Do not use placeholder XML from GitHub or the public Static Site."
    )


def build_welcome_text(*, manifest_url: str) -> str:
    return f"""You're approved for the Wealth Blotter Plus pilot.

Next steps (about 10 minutes, desktop Excel only — not Excel Online):

{_manifest_step_text(manifest_url)}
2) Load it once in desktop Excel
   Insert → Add-ins → My Add-ins → Upload My Add-in
   (Mac: if Upload is missing, copy the file into
   ~/Library/Containers/com.microsoft.Excel/Data/Documents/wef/
   then Quit Excel and reopen — open Copilot from the ribbon.)
   Do NOT use Developer → Excel Add-ins (that is for Analysis ToolPak only).

3) Open the Wealth Blotter / Copilot pane from the ribbon.

4) Sign in + LLM setup (in the task pane)
   Use the SAME email and password you created on the request-access page.

   Primary LLM (required for AI features):
   - Choose provider (Ollama, OpenAI, Anthropic, DeepSeek, Grok, Gemini, …)
   - Enter an API key AND/OR a base URL (e.g. Ollama http://127.0.0.1:11434)
   - Primary is the workhorse for agents (including research / RAG).

   Secondary LLM (optional):
   - Another provider API key if you have one.
   - If blank, everything uses primary.

   Keys stay on your computer and are not stored on our servers.
   Note: a laptop Ollama URL only works when the API can reach that machine
   (local API / developer setup). Hosted cloud API cannot call your localhost.

5) Try a small portfolio
   Paste a table with columns:
   client_alias | ticker | quantity | asset_class | currency
   Or open the attached starter CSV in Excel, then select the table and follow Copilot.

You will not receive a password from us — only you know the password you registered with.

Questions? Reply to the host who approved you.
"""


def build_welcome_html(*, manifest_url: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<body style="font-family:Segoe UI,Helvetica,Arial,sans-serif;line-height:1.45;color:#111;">
  <h2 style="margin:0 0 12px;">You're approved for Wealth Blotter Plus</h2>
  <p>Use desktop Excel (Mac or Windows). Excel Online is not supported for this pilot.</p>
  <ol>
    <li style="margin-bottom:10px;">
      {_manifest_step_html(manifest_url)}
    </li>
    <li style="margin-bottom:10px;">
      <strong>Load it once in Excel</strong><br/>
      Insert → Add-ins → My Add-ins → Upload My Add-in<br/>
      Mac tip: if Upload is missing, copy the file into
      <code>~/Library/Containers/com.microsoft.Excel/Data/Documents/wef/</code>,
      Quit Excel, reopen, then open Copilot.
      Do not use Developer → Excel Add-ins (ToolPak only).
    </li>
    <li style="margin-bottom:10px;">
      Open the <strong>Wealth Blotter / Copilot</strong> pane from the ribbon.
    </li>
    <li style="margin-bottom:10px;">
      <strong>Sign in</strong> with the same email/password you used at request-access.<br/>
      <strong>Primary LLM (required for AI):</strong> provider + API key
      <em>and/or</em> base URL (e.g. Ollama <code>http://127.0.0.1:11434</code>).
      This is the workhorse for agents (including research / RAG).<br/>
      <strong>Secondary LLM (optional):</strong> OpenAI, Anthropic, DeepSeek, Grok,
      Gemini, etc. If blank, everything uses primary.<br/>
      Keys are not stored on our servers. Laptop Ollama only works when the API
      can reach your machine (local/dev); a cloud-hosted API cannot call localhost.
    </li>
    <li style="margin-bottom:10px;">
      Paste a small holdings table (or open the attached starter CSV), select it,
      and follow Copilot through import → scenario → optimize → place orders.
    </li>
  </ol>
  <p style="color:#555;font-size:13px;">
    We never email your password. Use the one you chose when requesting access.
  </p>
</body>
</html>
"""


def send_pilot_welcome_email(
    *,
    to_email: str,
    settings: Settings | None = None,
) -> WelcomeEmailResult:
    """Send one welcome email via Resend. Raises WelcomeEmailError on failure."""
    cfg = settings or get_settings()
    api_key = (cfg.resend_api_key or "").strip()
    from_addr = (cfg.pilot_welcome_from_email or "").strip()
    if not api_key:
        raise WelcomeEmailError(
            "RESEND_API_KEY is not configured on the server."
        )
    if not from_addr:
        raise WelcomeEmailError(
            "PILOT_WELCOME_FROM_EMAIL is not configured on the server."
        )

    email_norm = to_email.strip().lower()
    manifest_url = default_manifest_url(cfg)
    subject = "You're approved — Wealth Blotter Plus pilot setup"
    text_body = build_welcome_text(manifest_url=manifest_url)
    html_body = build_welcome_html(manifest_url=manifest_url)
    csv_b64 = base64.b64encode(STARTER_CSV.encode("utf-8")).decode("ascii")

    payload = {
        "from": from_addr,
        "to": [email_norm],
        "subject": subject,
        "text": text_body,
        "html": html_body,
        "attachments": [
            {
                "filename": "wealth_blotter_starter_portfolio.csv",
                "content": csv_b64,
            }
        ],
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            res = client.post(
                RESEND_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        logger.error("pilot_welcome_email_network", error=str(exc), to=email_norm)
        raise WelcomeEmailError(
            f"Could not reach email provider: {type(exc).__name__}"
        ) from exc

    if res.status_code >= 400:
        detail = (res.text or "")[:400]
        logger.error(
            "pilot_welcome_email_rejected",
            status=res.status_code,
            detail=detail,
            to=email_norm,
        )
        raise WelcomeEmailError(
            f"Email provider rejected send (HTTP {res.status_code}). "
            "Check RESEND_API_KEY and verified From domain."
        )

    provider_id: str | None = None
    try:
        body = res.json()
        if isinstance(body, dict):
            raw_id = body.get("id")
            provider_id = str(raw_id) if raw_id else None
    except Exception:  # noqa: BLE001 — non-fatal parse
        provider_id = None

    logger.info(
        "pilot_welcome_email_sent",
        to=email_norm,
        provider_id=provider_id,
        manifest_url=manifest_url,
    )
    return WelcomeEmailResult(provider_id=provider_id, to_email=email_norm)
