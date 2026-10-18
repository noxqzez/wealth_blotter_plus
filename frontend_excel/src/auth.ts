/**
 * Pilot auth + BYOK helpers for Excel task pane.
 * Tokens/keys stay in browser localStorage — never commit real values.
 */

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";

export const TOKEN_KEY = "sb_access_token";
export const REFRESH_KEY = "sb_refresh_token";
export const TOKEN_EXPIRES_AT_KEY = "sb_access_expires_at";
export const SESSION_EMAIL_KEY = "wbp_session_email";
/** @deprecated migrated to PRIMARY_KEY */
export const LLM_KEY = "wbp_llm_api_key";
/** @deprecated migrated to PRIMARY_PROVIDER */
export const LLM_PROVIDER_KEY = "wbp_llm_provider";

export const PRIMARY_KEY = "wbp_llm_primary_key";
export const PRIMARY_PROVIDER = "wbp_llm_primary_provider";
export const PRIMARY_BASE_URL = "wbp_llm_primary_base_url";
export const SECONDARY_KEY = "wbp_llm_secondary_key";
export const SECONDARY_PROVIDER = "wbp_llm_secondary_provider";

export type LlmSlotConfig = {
  apiKey: string;
  provider: string;
  baseUrl: string;
};

export function getAccessToken(): string {
  return (localStorage.getItem(TOKEN_KEY) || "").trim();
}

function migrateLegacyByok(): void {
  if (!localStorage.getItem(PRIMARY_KEY) && localStorage.getItem(LLM_KEY)) {
    localStorage.setItem(PRIMARY_KEY, localStorage.getItem(LLM_KEY) || "");
  }
  if (!localStorage.getItem(PRIMARY_PROVIDER) && localStorage.getItem(LLM_PROVIDER_KEY)) {
    localStorage.setItem(
      PRIMARY_PROVIDER,
      localStorage.getItem(LLM_PROVIDER_KEY) || "openai",
    );
  }
}

export function getPrimaryLlm(): LlmSlotConfig {
  migrateLegacyByok();
  return {
    apiKey: (localStorage.getItem(PRIMARY_KEY) || "").trim(),
    provider:
      (localStorage.getItem(PRIMARY_PROVIDER) || "ollama").trim() || "ollama",
    baseUrl: (localStorage.getItem(PRIMARY_BASE_URL) || "").trim(),
  };
}

export function getSecondaryLlm(): LlmSlotConfig {
  return {
    apiKey: (localStorage.getItem(SECONDARY_KEY) || "").trim(),
    provider: (localStorage.getItem(SECONDARY_PROVIDER) || "").trim(),
    baseUrl: "",
  };
}

/** @deprecated use getPrimaryLlm().apiKey */
export function getLlmApiKey(): string {
  return getPrimaryLlm().apiKey;
}

/** @deprecated use getPrimaryLlm().provider */
export function getLlmProvider(): string {
  return getPrimaryLlm().provider;
}

export function authHeaders(correlationId: string): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Request-ID": correlationId,
  };
  const token = getAccessToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  const primary = getPrimaryLlm();
  if (primary.apiKey || primary.baseUrl) {
    if (primary.apiKey) headers["X-LLM-API-Key"] = primary.apiKey;
    headers["X-LLM-Provider"] = primary.provider || "ollama";
    if (primary.baseUrl) headers["X-LLM-Base-URL"] = primary.baseUrl;
  }

  const secondary = getSecondaryLlm();
  if (secondary.apiKey && secondary.provider) {
    headers["X-LLM-Secondary-API-Key"] = secondary.apiKey;
    headers["X-LLM-Secondary-Provider"] = secondary.provider;
  }
  return headers;
}

export async function registerPilot(
  email: string,
  password: string,
): Promise<{ message: string }> {
  const res = await fetch(`${API_BASE}/api/v1/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const text = await res.text();
  let body: { detail?: unknown; message?: string } = {};
  try {
    body = JSON.parse(text) as typeof body;
  } catch {
    /* raw */
  }
  if (!res.ok) {
    const d = body.detail;
    const msg =
      typeof d === "object" && d && "message" in d
        ? String((d as { message: string }).message)
        : typeof d === "string"
          ? d
          : text || "Registration failed";
    throw new Error(msg);
  }
  return { message: body.message || "Registered — awaiting approval." };
}

function persistSession(body: {
  access_token: string;
  refresh_token?: string | null;
  expires_in?: number | null;
}): void {
  localStorage.setItem(TOKEN_KEY, body.access_token);
  const refresh = (body.refresh_token || "").trim();
  if (refresh) localStorage.setItem(REFRESH_KEY, refresh);
  const expiresIn = Number(body.expires_in);
  if (Number.isFinite(expiresIn) && expiresIn > 0) {
    // Refresh 60s before expiry when possible.
    const skewMs = Math.min(60_000, Math.max(5_000, expiresIn * 1000 * 0.1));
    localStorage.setItem(
      TOKEN_EXPIRES_AT_KEY,
      String(Date.now() + expiresIn * 1000 - skewMs),
    );
  } else {
    localStorage.removeItem(TOKEN_EXPIRES_AT_KEY);
  }
}

export async function loginPilot(
  email: string,
  password: string,
): Promise<{ access_token: string; email: string }> {
  const res = await fetch(`${API_BASE}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const text = await res.text();
  let body: {
    detail?: unknown;
    access_token?: string;
    refresh_token?: string | null;
    expires_in?: number | null;
    email?: string;
  } = {};
  try {
    body = JSON.parse(text) as typeof body;
  } catch {
    /* raw */
  }
  if (!res.ok) {
    const d = body.detail;
    const msg =
      typeof d === "object" && d && "message" in d
        ? String((d as { message: string }).message)
        : typeof d === "string"
          ? d
          : text || "Login failed";
    throw new Error(msg);
  }
  if (!body.access_token) throw new Error("Login failed (no token).");
  const emailOut = body.email || email;
  persistSession({
    access_token: body.access_token,
    refresh_token: body.refresh_token,
    expires_in: body.expires_in,
  });
  if (emailOut) localStorage.setItem(SESSION_EMAIL_KEY, emailOut);
  return { access_token: body.access_token, email: emailOut };
}

export function getRefreshToken(): string {
  return (localStorage.getItem(REFRESH_KEY) || "").trim();
}

/** Renew access_token using stored refresh_token. Returns true on success. */
export async function refreshPilotSession(): Promise<boolean> {
  const refresh = getRefreshToken();
  if (!refresh) return false;
  try {
    const res = await fetch(`${API_BASE}/api/v1/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
    });
    const text = await res.text();
    let body: {
      access_token?: string;
      refresh_token?: string | null;
      expires_in?: number | null;
    } = {};
    try {
      body = JSON.parse(text) as typeof body;
    } catch {
      /* raw */
    }
    if (!res.ok || !body.access_token) return false;
    persistSession({
      access_token: body.access_token,
      refresh_token: body.refresh_token || refresh,
      expires_in: body.expires_in,
    });
    return true;
  } catch {
    return false;
  }
}

/** Refresh proactively when close to expiry (best-effort). */
export async function ensureFreshSession(): Promise<void> {
  const raw = localStorage.getItem(TOKEN_EXPIRES_AT_KEY);
  if (!raw) return;
  const expiresAt = Number(raw);
  if (!Number.isFinite(expiresAt) || Date.now() < expiresAt) return;
  await refreshPilotSession();
}

export function logoutPilot(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REFRESH_KEY);
  localStorage.removeItem(TOKEN_EXPIRES_AT_KEY);
  localStorage.removeItem(SESSION_EMAIL_KEY);
}

/** Collapse the big login/LLM form after sign-in so chat has room. */
export function setPilotAuthCollapsed(
  collapsed: boolean,
  email?: string | null,
): void {
  const root = document.getElementById("pilotAuth");
  const barEmail = document.getElementById("pilotAuthBarEmail");
  const collapseBtn = document.getElementById(
    "pilotAuthCollapseBtn",
  ) as HTMLButtonElement | null;
  if (!root) return;
  root.classList.toggle("collapsed", collapsed);
  const label =
    (email || "").trim() ||
    (localStorage.getItem(SESSION_EMAIL_KEY) || "").trim() ||
    "advisor";
  if (barEmail) barEmail.textContent = label;
  if (collapseBtn) collapseBtn.hidden = !collapsed;
}

/**
 * Clear JWT and surface the Pilot access panel when the API returns 401.
 * Tries one silent refresh first. Returns true when the status was handled.
 */
export async function handleUnauthorized(status: number): Promise<boolean> {
  if (status !== 401) return false;
  const refreshed = await refreshPilotSession();
  if (refreshed) return false; // caller should retry
  logoutPilot();
  setPilotAuthCollapsed(false);
  const statusEl = document.getElementById("pilotAuthStatus");
  if (statusEl) {
    statusEl.textContent = "Session expired — sign in again.";
    statusEl.classList.add("error");
  }
  document.getElementById("pilotAuth")?.scrollIntoView({
    behavior: "smooth",
    block: "nearest",
  });
  return true;
}

export function saveByokConfig(opts: {
  primaryKey: string;
  primaryProvider: string;
  primaryBaseUrl: string;
  secondaryKey: string;
  secondaryProvider: string;
}): void {
  const pk = opts.primaryKey.trim();
  const pb = opts.primaryBaseUrl.trim();
  const pp = opts.primaryProvider.trim() || "ollama";
  if (pk) localStorage.setItem(PRIMARY_KEY, pk);
  else localStorage.removeItem(PRIMARY_KEY);
  localStorage.setItem(PRIMARY_PROVIDER, pp);
  if (pb) localStorage.setItem(PRIMARY_BASE_URL, pb);
  else localStorage.removeItem(PRIMARY_BASE_URL);

  // Keep legacy keys in sync for any leftover readers.
  if (pk) localStorage.setItem(LLM_KEY, pk);
  else localStorage.removeItem(LLM_KEY);
  localStorage.setItem(LLM_PROVIDER_KEY, pp);

  const sk = opts.secondaryKey.trim();
  const sp = opts.secondaryProvider.trim();
  if (sk && sp) {
    localStorage.setItem(SECONDARY_KEY, sk);
    localStorage.setItem(SECONDARY_PROVIDER, sp);
  } else {
    localStorage.removeItem(SECONDARY_KEY);
    localStorage.removeItem(SECONDARY_PROVIDER);
  }
}

/** @deprecated use saveByokConfig */
export function saveByok(apiKey: string, provider: string): void {
  const cur = getPrimaryLlm();
  saveByokConfig({
    primaryKey: apiKey,
    primaryProvider: provider,
    primaryBaseUrl: cur.baseUrl,
    secondaryKey: getSecondaryLlm().apiKey,
    secondaryProvider: getSecondaryLlm().provider,
  });
}

export function primaryLlmConfigured(): boolean {
  const p = getPrimaryLlm();
  return Boolean(p.apiKey || p.baseUrl);
}

export async function fetchSsoStatus(): Promise<{
  enabled: boolean;
  providers: string[];
}> {
  try {
    const res = await fetch(`${API_BASE}/api/v1/auth/sso/status`);
    if (!res.ok) return { enabled: false, providers: [] };
    const body = (await res.json()) as {
      enabled?: boolean;
      providers?: string[];
    };
    return {
      enabled: Boolean(body.enabled),
      providers: Array.isArray(body.providers) ? body.providers : [],
    };
  } catch {
    return { enabled: false, providers: [] };
  }
}

async function startSsoAuthorize(provider: string): Promise<string> {
  const res = await fetch(
    `${API_BASE}/api/v1/auth/sso/authorize?provider=${encodeURIComponent(provider)}`,
  );
  const text = await res.text();
  let body: { authorize_url?: string; detail?: unknown } = {};
  try {
    body = JSON.parse(text) as typeof body;
  } catch {
    /* raw */
  }
  if (!res.ok || !body.authorize_url) {
    const d = body.detail;
    const msg =
      typeof d === "object" && d && "message" in d
        ? String((d as { message: string }).message)
        : typeof d === "string"
          ? d
          : text || "SSO is not available.";
    throw new Error(msg);
  }
  return body.authorize_url;
}

function openSsoDialog(
  authorizeUrl: string,
  onResult: (payload: {
    ok: boolean;
    access_token?: string;
    email?: string;
    error?: string;
  }) => void,
): void {
  const officeUi = (
    window as unknown as {
      Office?: {
        context?: {
          ui?: {
            displayDialogAsync?: (
              url: string,
              opts: { height: number; width: number; displayInIframe: boolean },
              cb: (asyncResult: {
                status: string;
                value?: {
                  addEventHandler: (
                    eventType: string,
                    handler: (arg: { message: string }) => void,
                  ) => void;
                  close: () => void;
                };
                error?: { message?: string };
              }) => void,
            ) => void;
          };
        };
      };
    }
  ).Office?.context?.ui;

  if (officeUi?.displayDialogAsync) {
    officeUi.displayDialogAsync(
      authorizeUrl,
      { height: 60, width: 40, displayInIframe: false },
      (asyncResult) => {
        if (asyncResult.status !== "succeeded" || !asyncResult.value) {
          onResult({
            ok: false,
            error: asyncResult.error?.message || "Could not open SSO dialog.",
          });
          return;
        }
        const dialog = asyncResult.value;
        dialog.addEventHandler("DialogMessageReceived", (arg) => {
          try {
            const payload = JSON.parse(arg.message) as {
              ok: boolean;
              access_token?: string;
              email?: string;
              error?: string;
            };
            onResult(payload);
          } catch {
            onResult({ ok: false, error: "Invalid SSO dialog response." });
          } finally {
            dialog.close();
          }
        });
      },
    );
    return;
  }

  window.open(authorizeUrl, "_blank", "noopener,noreferrer");
  onResult({
    ok: false,
    error:
      "Opened SSO in a browser window. If sign-in succeeds there, return here and use password login or retry from Excel.",
  });
}

function readByokFromForm(): {
  primaryKey: string;
  primaryProvider: string;
  primaryBaseUrl: string;
  secondaryKey: string;
  secondaryProvider: string;
} {
  const primaryKey =
    (document.getElementById("pilotPrimaryKey") as HTMLInputElement | null)
      ?.value || "";
  const primaryProvider =
    (document.getElementById("pilotPrimaryProvider") as HTMLSelectElement | null)
      ?.value || "ollama";
  const primaryBaseUrl =
    (document.getElementById("pilotPrimaryBaseUrl") as HTMLInputElement | null)
      ?.value || "";
  const secondaryKey =
    (document.getElementById("pilotSecondaryKey") as HTMLInputElement | null)
      ?.value || "";
  const secondaryProvider =
    (
      document.getElementById("pilotSecondaryProvider") as HTMLSelectElement | null
    )?.value || "";
  return {
    primaryKey,
    primaryProvider,
    primaryBaseUrl,
    secondaryKey,
    secondaryProvider,
  };
}

export function wirePilotAuthPanel(): void {
  const emailEl = document.getElementById("pilotEmail") as HTMLInputElement | null;
  const passEl = document.getElementById("pilotPassword") as HTMLInputElement | null;
  const primaryKeyEl = document.getElementById(
    "pilotPrimaryKey",
  ) as HTMLInputElement | null;
  const primaryProviderEl = document.getElementById(
    "pilotPrimaryProvider",
  ) as HTMLSelectElement | null;
  const primaryBaseEl = document.getElementById(
    "pilotPrimaryBaseUrl",
  ) as HTMLInputElement | null;
  const secondaryKeyEl = document.getElementById(
    "pilotSecondaryKey",
  ) as HTMLInputElement | null;
  const secondaryProviderEl = document.getElementById(
    "pilotSecondaryProvider",
  ) as HTMLSelectElement | null;
  const statusEl = document.getElementById("pilotAuthStatus");
  const registerBtn = document.getElementById("pilotRegisterBtn");
  const loginBtn = document.getElementById("pilotLoginBtn");
  const logoutBtn = document.getElementById("pilotLogoutBtn");
  const barLogoutBtn = document.getElementById("pilotAuthBarLogoutBtn");
  const expandBtn = document.getElementById("pilotAuthExpandBtn");
  const collapseBtn = document.getElementById("pilotAuthCollapseBtn");
  const ssoBox = document.getElementById("pilotSsoBox");
  const ssoActions = document.getElementById("pilotSsoActions");

  const setMsg = (text: string, isError = false) => {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.classList.toggle("error", isError);
  };

  const persistForm = () => saveByokConfig(readByokFromForm());

  const doLogout = () => {
    logoutPilot();
    setPilotAuthCollapsed(false);
    setMsg("Signed out.");
  };

  const primary = getPrimaryLlm();
  const secondary = getSecondaryLlm();
  if (primaryKeyEl) primaryKeyEl.value = primary.apiKey;
  if (primaryProviderEl) primaryProviderEl.value = primary.provider || "ollama";
  if (primaryBaseEl) {
    primaryBaseEl.value =
      primary.baseUrl ||
      (primary.provider === "ollama" ? "http://127.0.0.1:11434" : "");
  }
  if (secondaryKeyEl) secondaryKeyEl.value = secondary.apiKey;
  if (secondaryProviderEl) secondaryProviderEl.value = secondary.provider || "";

  const savedEmail =
    (localStorage.getItem(SESSION_EMAIL_KEY) || "").trim() ||
    (emailEl?.value || "").trim();
  if (emailEl && savedEmail && !emailEl.value) emailEl.value = savedEmail;

  if (getAccessToken()) {
    setMsg(`Signed in as ${savedEmail || "advisor"}`);
    setPilotAuthCollapsed(true, savedEmail || "advisor");
  } else {
    setPilotAuthCollapsed(false);
  }

  void (async () => {
    const sso = await fetchSsoStatus();
    if (!sso.enabled || !sso.providers.length || !ssoBox || !ssoActions) {
      return;
    }
    ssoBox.hidden = false;
    for (const provider of sso.providers) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "secondary";
      btn.textContent = `SSO: ${provider}`;
      btn.addEventListener("click", async () => {
        try {
          persistForm();
          setMsg("Starting SSO…");
          await startSsoAuthorize(provider);
          const startUrl = `${API_BASE}/auth/sso/start?provider=${encodeURIComponent(provider)}`;
          openSsoDialog(startUrl, (payload) => {
            if (!payload.ok || !payload.access_token) {
              setMsg(payload.error || "SSO cancelled or failed.", true);
              setPilotAuthCollapsed(false);
              return;
            }
            localStorage.setItem(TOKEN_KEY, payload.access_token);
            const em = payload.email || "advisor";
            localStorage.setItem(SESSION_EMAIL_KEY, em);
            setMsg(`Signed in via SSO as ${em}`);
            setPilotAuthCollapsed(true, em);
          });
        } catch (err) {
          setMsg(err instanceof Error ? err.message : String(err), true);
          setPilotAuthCollapsed(false);
        }
      });
      ssoActions.appendChild(btn);
    }
  })();

  registerBtn?.addEventListener("click", async () => {
    try {
      const email = (emailEl?.value || "").trim();
      const password = passEl?.value || "";
      if (!email || !password) {
        setMsg("Email and password required.", true);
        return;
      }
      const out = await registerPilot(email, password);
      setMsg(out.message);
      setPilotAuthCollapsed(false);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : String(err), true);
    }
  });

  loginBtn?.addEventListener("click", async () => {
    try {
      const email = (emailEl?.value || "").trim();
      const password = passEl?.value || "";
      if (!email || !password) {
        setMsg("Email and password required.", true);
        return;
      }
      persistForm();
      if (!primaryLlmConfigured()) {
        setMsg(
          "Primary LLM required: enter an API key and/or base URL (e.g. Ollama http://127.0.0.1:11434).",
          true,
        );
        return;
      }
      const out = await loginPilot(email, password);
      setMsg(`Signed in as ${out.email}`);
      setPilotAuthCollapsed(true, out.email);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : String(err), true);
      setPilotAuthCollapsed(false);
    }
  });

  logoutBtn?.addEventListener("click", doLogout);
  barLogoutBtn?.addEventListener("click", doLogout);

  expandBtn?.addEventListener("click", () => {
    setPilotAuthCollapsed(false);
    setMsg("Settings open — hide when finished.");
  });
  collapseBtn?.addEventListener("click", () => {
    if (!getAccessToken()) {
      setMsg("Sign in first to hide settings.", true);
      return;
    }
    persistForm();
    const em =
      (localStorage.getItem(SESSION_EMAIL_KEY) || "").trim() ||
      (emailEl?.value || "").trim() ||
      "advisor";
    setPilotAuthCollapsed(true, em);
  });

  for (const el of [
    primaryKeyEl,
    primaryProviderEl,
    primaryBaseEl,
    secondaryKeyEl,
    secondaryProviderEl,
  ]) {
    el?.addEventListener("change", () => persistForm());
  }
}
