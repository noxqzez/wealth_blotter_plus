/**
 * Pilot auth + BYOK helpers for Excel task pane.
 * Tokens/keys stay in browser localStorage — never commit real values.
 */

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";

export const TOKEN_KEY = "sb_access_token";
export const LLM_KEY = "wbp_llm_api_key";
export const LLM_PROVIDER_KEY = "wbp_llm_provider";

export function getAccessToken(): string {
  return (localStorage.getItem(TOKEN_KEY) || "").trim();
}

export function getLlmApiKey(): string {
  return (localStorage.getItem(LLM_KEY) || "").trim();
}

export function getLlmProvider(): string {
  return (localStorage.getItem(LLM_PROVIDER_KEY) || "openai").trim() || "openai";
}

export function authHeaders(correlationId: string): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Request-ID": correlationId,
  };
  const token = getAccessToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const llmKey = getLlmApiKey();
  if (llmKey) {
    headers["X-LLM-API-Key"] = llmKey;
    headers["X-LLM-Provider"] = getLlmProvider();
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
  localStorage.setItem(TOKEN_KEY, body.access_token);
  return { access_token: body.access_token, email: body.email || email };
}

export function logoutPilot(): void {
  localStorage.removeItem(TOKEN_KEY);
}

/**
 * Clear JWT and surface the Pilot access panel when the API returns 401.
 * Returns true when the status was handled as unauthorized.
 */
export function handleUnauthorized(status: number): boolean {
  if (status !== 401) return false;
  logoutPilot();
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

export function saveByok(apiKey: string, provider: string): void {
  if (apiKey.trim()) localStorage.setItem(LLM_KEY, apiKey.trim());
  else localStorage.removeItem(LLM_KEY);
  localStorage.setItem(LLM_PROVIDER_KEY, provider.trim() || "openai");
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

  // Fallback outside Office: open authorize URL (callback page completes flow).
  window.open(authorizeUrl, "_blank", "noopener,noreferrer");
  onResult({
    ok: false,
    error:
      "Opened SSO in a browser window. If sign-in succeeds there, return here and use password login or retry from Excel.",
  });
}

export function wirePilotAuthPanel(): void {
  const emailEl = document.getElementById("pilotEmail") as HTMLInputElement | null;
  const passEl = document.getElementById("pilotPassword") as HTMLInputElement | null;
  const keyEl = document.getElementById("pilotLlmKey") as HTMLInputElement | null;
  const providerEl = document.getElementById(
    "pilotLlmProvider",
  ) as HTMLSelectElement | null;
  const statusEl = document.getElementById("pilotAuthStatus");
  const registerBtn = document.getElementById("pilotRegisterBtn");
  const loginBtn = document.getElementById("pilotLoginBtn");
  const logoutBtn = document.getElementById("pilotLogoutBtn");
  const ssoBox = document.getElementById("pilotSsoBox");
  const ssoActions = document.getElementById("pilotSsoActions");

  const setMsg = (text: string, isError = false) => {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.classList.toggle("error", isError);
  };

  if (keyEl) keyEl.value = getLlmApiKey();
  if (providerEl) providerEl.value = getLlmProvider();
  if (getAccessToken()) setMsg("Signed in (token stored).");

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
          saveByok(keyEl?.value || "", providerEl?.value || "openai");
          setMsg("Starting SSO…");
          // Fail fast with API error message when SSO is off / misconfigured.
          await startSsoAuthorize(provider);
          const startUrl = `${API_BASE}/auth/sso/start?provider=${encodeURIComponent(provider)}`;
          openSsoDialog(startUrl, (payload) => {
            if (!payload.ok || !payload.access_token) {
              setMsg(payload.error || "SSO cancelled or failed.", true);
              return;
            }
            localStorage.setItem(TOKEN_KEY, payload.access_token);
            setMsg(`Signed in via SSO as ${payload.email || "advisor"}`);
          });
        } catch (err) {
          setMsg(err instanceof Error ? err.message : String(err), true);
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
      saveByok(keyEl?.value || "", providerEl?.value || "openai");
      const out = await loginPilot(email, password);
      setMsg(`Signed in as ${out.email}`);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : String(err), true);
    }
  });

  logoutBtn?.addEventListener("click", () => {
    logoutPilot();
    setMsg("Signed out.");
  });

  keyEl?.addEventListener("change", () => {
    saveByok(keyEl.value, providerEl?.value || "openai");
  });
  providerEl?.addEventListener("change", () => {
    saveByok(keyEl?.value || "", providerEl.value);
  });
}
