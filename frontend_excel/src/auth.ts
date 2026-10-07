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

export function saveByok(apiKey: string, provider: string): void {
  if (apiKey.trim()) localStorage.setItem(LLM_KEY, apiKey.trim());
  else localStorage.removeItem(LLM_KEY);
  localStorage.setItem(LLM_PROVIDER_KEY, provider.trim() || "openai");
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

  const setMsg = (text: string, isError = false) => {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.classList.toggle("error", isError);
  };

  if (keyEl) keyEl.value = getLlmApiKey();
  if (providerEl) providerEl.value = getLlmProvider();
  if (getAccessToken()) setMsg("Signed in (token stored).");

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
