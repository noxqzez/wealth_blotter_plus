/**
 * Excel task pane entry — Office.onReady + sanitized render helpers.
 * Auth: store Supabase JWT in secure local storage; send Authorization Bearer.
 */

import DOMPurify from "dompurify";

const statusEl = document.getElementById("status");
const outputEl = document.getElementById("output");
const analyzeBtn = document.getElementById("analyze");

function setStatus(text: string): void {
  if (statusEl) {
    statusEl.textContent = text;
  }
}

/** Sanitize any LLM/API text before DOM insertion. */
export function sanitizeForDom(dirty: string): string {
  return DOMPurify.sanitize(dirty, { ALLOWED_TAGS: [], ALLOWED_ATTR: [] });
}

function renderSafe(text: string): void {
  if (outputEl) {
    outputEl.textContent = sanitizeForDom(text);
  }
}

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

async function stubAnalyze(): Promise<void> {
  setStatus("Calling API stub…");
  // Token must come from Supabase Auth — never hardcode secrets here.
  const token = localStorage.getItem("sb_access_token");
  if (!token) {
    renderSafe("No Supabase access token in localStorage (sb_access_token). Wire Auth next.");
    setStatus("Auth required");
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/api/v1/health`);
    const body = await res.text();
    renderSafe(body);
    setStatus(res.ok ? "Health OK" : `Health failed (${res.status})`);
  } catch (err) {
    renderSafe(err instanceof Error ? err.message : "Request failed");
    setStatus("Error");
  }
}

Office.onReady((info) => {
  if (info.host === Office.HostType.Excel) {
    setStatus("Excel task pane ready (scaffold)");
    if (analyzeBtn) {
      analyzeBtn.disabled = false;
      analyzeBtn.addEventListener("click", () => {
        void stubAnalyze();
      });
    }
  } else {
    setStatus(`Host is ${info.host ?? "unknown"} — open inside Excel for full behavior`);
  }
});
