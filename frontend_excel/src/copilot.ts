/**
 * Excel Copilot chat workspace — session hub; Excel writes only via excel_writes.
 */

import DOMPurify from "dompurify";
import { authHeaders, wirePilotAuthPanel } from "./auth";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";

const IMPORT_TIMEOUT_MS = 30_000;
const SCENARIO_TIMEOUT_MS = 120_000;
const PDF_TIMEOUT_MS = 30_000;
const DEFAULT_TIMEOUT_MS = 30_000;

const LEGACY_SCENARIO_KEYS = [
  "wbp_portfolio_id",
  "wbp_scenario_run_id",
  "wbp_scenario_params",
] as const;

const SELECTION_ACTION_IDS = new Set([
  "process_security_list",
  "reprocess_corrected_list",
]);

const ORD_BLOTTER_ACTION_IDS = new Set(["place_all_orders"]);
const ORD_BLOTTER_SHEET = "ord_blotter";

const SUPPORT_MESSAGE =
  "System error, please try again later and raise to support.";

const ErrorCode = {
  TIMEOUT: "WBP-COPILOT-TIMEOUT",
  SERVER: "WBP-COPILOT-SERVER",
  NETWORK: "WBP-COPILOT-NETWORK",
  CLIENT: "WBP-COPILOT-CLIENT",
  PARSE: "WBP-COPILOT-PARSE",
} as const;

// ---------------------------------------------------------------------------
// Types (mirrors backend/app/schemas/copilot.py)
// ---------------------------------------------------------------------------

type ActionVariant = "primary" | "secondary" | "danger";
type GuardrailSeverity = "info" | "warning" | "blocking_error";

export type ActionTrigger = {
  id: string;
  label: string;
  variant?: ActionVariant;
  requires_excel_selection?: boolean;
  requires_user_text?: boolean;
};

export type GuardrailCheck = {
  rule_id: string;
  severity: GuardrailSeverity;
  message: string;
  suggested_fix?: string | null;
};

export type ExcelWriteSpec = {
  sheet_name: string;
  values: unknown[][];
  activate?: boolean;
  hide_source_sheet?: string | null;
  column_number_formats?: Record<string, string>;
};

export type DownloadArtifact = {
  kind: "pdf";
  filename: string;
  content_base64: string;
};

export type CopilotAgentResponse = {
  message_id: string;
  session_id: string;
  current_state: string;
  status_label?: string;
  content: string;
  guardrails?: GuardrailCheck[];
  actions?: ActionTrigger[];
  execution_status_badges?: string[];
  excel_writes?: ExcelWriteSpec[];
  download?: DownloadArtifact | null;
  accept_user_text?: boolean;
  user_text_placeholder?: string | null;
  show_star_rating?: boolean;
};

/** Fallback labels if an older API omits status_label. */
const WORKFLOW_STATUS_LABELS: Record<string, string> = {
  INIT: "Import securities",
  BLOTTER_HAS_ERRORS: "Fix import errors",
  BLOTTER_VALIDATED: "Blotter validated",
  PORTFOLIO_ENRICHED: "Enter scenario",
  SCENARIO_PARSED: "Review scenario",
  OPTIMIZATION_COMPLETE: "Optimization complete",
  ORDERS_PLACED: "Orders placed",
};

function friendlyStatusLabel(resp: CopilotAgentResponse): string {
  if (resp.status_label && resp.status_label.trim()) {
    return resp.status_label.trim();
  }
  const key = (resp.current_state || "").trim();
  return WORKFLOW_STATUS_LABELS[key] || key;
}

type PasteRow = {
  client_alias: string;
  ticker: string;
  quantity: number | null;
  asset_class: string;
  currency: string | null;
};

type OrderBlotterRow = {
  order_id: string;
  client_alias?: string;
  status?: string;
  ticker: string;
  asset_class?: string;
  side: string;
  quantity: number | null;
  exec_type?: string;
};

type PasteSelection = {
  rows: PasteRow[];
  sourceSheetName: string;
};

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------

const els = {
  transcript: document.getElementById("transcript"),
  statusBadges: document.getElementById("statusBadges"),
  guardrails: document.getElementById("guardrails"),
  actions: document.getElementById("actions"),
  composer: document.getElementById("composer"),
  userText: document.getElementById("userText") as HTMLTextAreaElement | null,
  sendBtn: document.getElementById("sendBtn") as HTMLButtonElement | null,
  status: document.getElementById("status"),
};

let sessionId: string | null = null;
let busy = false;
let lastActions: ActionTrigger[] = [];
let showStarRating = false;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function sanitize(dirty: string): string {
  return DOMPurify.sanitize(dirty, { ALLOWED_TAGS: [], ALLOWED_ATTR: [] });
}

/** Lightweight markdown for assistant bubbles (bold, code, breaks). */
function formatAssistantHtml(raw: string): string {
  const escaped = sanitize(raw);
  const withMd = escaped
    .replace(/^###\s+(.+)$/gm, "<strong>$1</strong>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    // Collapse 3+ blank lines, then turn newlines into <br>
    .replace(/\n{3,}/g, "\n\n")
    .replace(/\n/g, "<br>");
  return DOMPurify.sanitize(withMd, {
    ALLOWED_TAGS: ["strong", "code", "br", "em"],
    ALLOWED_ATTR: [],
  });
}

function newCorrelationId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `local-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function shortRef(correlationId: string): string {
  return correlationId.replace(/-/g, "").slice(0, 8);
}

function supportMessage(errorCode: string, correlationId: string): string {
  return `${SUPPORT_MESSAGE}\nRef: ${errorCode} · ${shortRef(correlationId)}`;
}

function isAbortError(err: unknown): boolean {
  return (
    (err instanceof DOMException && err.name === "AbortError") ||
    (err instanceof Error && err.name === "AbortError")
  );
}

function setStatus(text: string, isError = false): void {
  if (!els.status) return;
  els.status.textContent = sanitize(text);
  els.status.classList.toggle("error", isError);
}

function clearLegacyScenarioKeys(): void {
  for (const key of LEGACY_SCENARIO_KEYS) {
    localStorage.removeItem(key);
  }
}

function timeoutForAction(actionId: string | null): number {
  if (!actionId) return DEFAULT_TIMEOUT_MS;
  const id = actionId.toLowerCase();
  if (id.includes("pdf") || id.includes("download")) return PDF_TIMEOUT_MS;
  if (
    id.includes("parse") ||
    id.includes("run") ||
    id.includes("scenario") ||
    id.includes("optim")
  ) {
    return SCENARIO_TIMEOUT_MS;
  }
  if (
    SELECTION_ACTION_IDS.has(actionId) ||
    id.includes("process") ||
    id.includes("import") ||
    id.includes("portfolio")
  ) {
    return IMPORT_TIMEOUT_MS;
  }
  return DEFAULT_TIMEOUT_MS;
}

type StructuredDetail = {
  error_code?: string;
  message?: string;
  correlation_id?: string;
  detail?: string;
};

function formatApiError(
  httpStatus: number,
  text: string,
  fallbackCode: string,
  correlationId: string,
): string {
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    const d = parsed.detail;
    if (typeof d === "string") {
      if (httpStatus >= 400 && httpStatus < 500) {
        return `${d}\nRef: ${fallbackCode} · ${shortRef(correlationId)}`;
      }
      return supportMessage(fallbackCode, correlationId);
    }
    if (d && typeof d === "object" && !Array.isArray(d)) {
      const body = d as StructuredDetail;
      const code = body.error_code || fallbackCode;
      const cid = body.correlation_id || correlationId;
      const msg = body.message || body.detail || "";
      if (httpStatus >= 500 || code === ErrorCode.SERVER) {
        return supportMessage(code, cid);
      }
      return `${msg}\nRef: ${code} · ${shortRef(cid)}`;
    }
    if (Array.isArray(d)) {
      const lines = d
        .map((item) =>
          typeof item === "object" && item && "msg" in item
            ? String((item as { msg: string }).msg)
            : JSON.stringify(item),
        )
        .join("\n");
      return `${lines}\nRef: ${fallbackCode} · ${shortRef(correlationId)}`;
    }
  } catch {
    // fall through
  }
  if (httpStatus >= 500) return supportMessage(fallbackCode, correlationId);
  return `${text || `Request failed (${httpStatus})`}\nRef: ${fallbackCode} · ${shortRef(correlationId)}`;
}

// ---------------------------------------------------------------------------
// UI render
// ---------------------------------------------------------------------------

function appendBubble(role: "assistant" | "user", content: string): void {
  if (!els.transcript) return;
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  const roleLabel = document.createElement("span");
  roleLabel.className = "role";
  roleLabel.textContent = role === "assistant" ? "Copilot" : "You";
  const body = document.createElement("div");
  body.className = "bubble-body";
  if (role === "assistant") {
    body.innerHTML = formatAssistantHtml(content);
  } else {
    body.textContent = sanitize(content);
  }
  bubble.appendChild(roleLabel);
  bubble.appendChild(body);
  els.transcript.appendChild(bubble);
  els.transcript.scrollTop = els.transcript.scrollHeight;
}

function renderBadges(badges: string[], pulsing = false): void {
  if (!els.statusBadges) return;
  els.statusBadges.replaceChildren();
  // One latest state only (avoid stacking SCENARIO_PARSED + RUN_OPTIMIZER, etc.).
  const latest = badges.filter(Boolean).slice(-1);
  for (const label of latest) {
    const chip = document.createElement("span");
    chip.className = pulsing ? "exec-badge pulse" : "exec-badge";
    chip.textContent = sanitize(label);
    els.statusBadges.appendChild(chip);
  }
}

function renderGuardrails(guardrails: GuardrailCheck[]): void {
  if (!els.guardrails) return;
  els.guardrails.replaceChildren();
  for (const g of guardrails) {
    const banner = document.createElement("div");
    const sev =
      g.severity === "blocking_error" || g.severity === "warning" || g.severity === "info"
        ? g.severity
        : "info";
    banner.className = `guard ${sev}`;
    let text = g.message;
    if (g.suggested_fix) text += `\nSuggested fix: ${g.suggested_fix}`;
    banner.textContent = sanitize(text);
    els.guardrails.appendChild(banner);
  }
}

function setComposer(accept: boolean, placeholder?: string | null): void {
  if (!els.composer || !els.userText || !els.sendBtn) return;
  if (accept) {
    els.composer.classList.remove("hidden");
    els.userText.disabled = busy;
    els.sendBtn.disabled = busy;
    els.userText.placeholder = placeholder?.trim() || "Type a message…";
  } else {
    els.composer.classList.add("hidden");
    els.userText.disabled = true;
    els.sendBtn.disabled = true;
    els.userText.value = "";
  }
}

function setBusy(next: boolean, inFlightLabel = "Working…"): void {
  busy = next;
  if (els.sendBtn) els.sendBtn.disabled = next || !els.composer || els.composer.classList.contains("hidden");
  if (els.userText) {
    els.userText.disabled =
      next || !els.composer || els.composer.classList.contains("hidden");
  }
  if (els.actions) {
    for (const btn of els.actions.querySelectorAll("button")) {
      (btn as HTMLButtonElement).disabled = next;
    }
  }
  if (next) {
    renderBadges([inFlightLabel], true);
  }
}

function renderStarRatingWidget(): void {
  if (!els.actions || !showStarRating) return;
  const wrap = document.createElement("div");
  wrap.className = "star-rating";
  wrap.setAttribute("role", "group");
  wrap.setAttribute("aria-label", "Rate this simulation 1 to 5 stars");
  const stars: HTMLButtonElement[] = [];

  const paint = (hot: number) => {
    stars.forEach((btn, i) => {
      const n = i + 1;
      btn.classList.toggle("is-hot", n <= hot);
      btn.classList.toggle("is-filled", n <= hot);
      btn.textContent = n <= hot ? "★" : "☆";
    });
  };

  for (let n = 1; n <= 5; n += 1) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "star";
    btn.textContent = "☆";
    btn.disabled = busy;
    btn.dataset.stars = String(n);
    btn.title = `${n} star${n === 1 ? "" : "s"}`;
    btn.setAttribute("aria-label", `Rate ${n} out of 5`);
    btn.addEventListener("mouseenter", () => paint(n));
    btn.addEventListener("focus", () => paint(n));
    btn.addEventListener("mouseleave", () => paint(0));
    btn.addEventListener("blur", () => paint(0));
    btn.addEventListener("click", () => {
      void handleAction({
        id: `rate_stars_${n}`,
        label: `${n}`,
        variant: "secondary",
      });
    });
    stars.push(btn);
    wrap.appendChild(btn);
  }
  els.actions.appendChild(wrap);
}

function renderActions(actions: ActionTrigger[]): void {
  lastActions = actions;
  if (!els.actions) return;
  els.actions.replaceChildren();
  if (showStarRating) {
    renderStarRatingWidget();
  }
  for (const action of actions) {
    // Star scores are handled by the compact widget, not separate buttons.
    if (action.id.startsWith("rate_stars_")) continue;
    const btn = document.createElement("button");
    btn.type = "button";
    const variant = action.variant ?? "primary";
    const classes: string[] = [];
    if (variant === "secondary" || variant === "danger") {
      classes.push(variant);
    }
    if (action.id === "skip_rating") {
      classes.push("rating");
    }
    if (classes.length) btn.className = classes.join(" ");
    btn.textContent = sanitize(action.label);
    btn.disabled = busy;
    btn.dataset.actionId = action.id;
    btn.title = sanitize(action.label);
    btn.addEventListener("click", () => {
      void handleAction(action);
    });
    els.actions.appendChild(btn);
  }
}

function applyResponseUi(resp: CopilotAgentResponse, opts?: { skipBubble?: boolean }): void {
  sessionId = resp.session_id;
  showStarRating = Boolean(resp.show_star_rating);
  if (!opts?.skipBubble && resp.content) {
    appendBubble("assistant", resp.content);
  }
  renderGuardrails(resp.guardrails ?? []);
  // Prefer friendly status_label (e.g. Import securities) over raw FSM enum.
  const stateLabel = friendlyStatusLabel(resp);
  renderBadges(stateLabel ? [stateLabel] : [], false);
  renderActions(resp.actions ?? []);
  setComposer(Boolean(resp.accept_user_text), resp.user_text_placeholder);
}

// ---------------------------------------------------------------------------
// Excel: selection + writes
// ---------------------------------------------------------------------------

function normalizeHeader(value: unknown): string {
  return String(value ?? "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "_");
}

async function readSelectedPasteRows(): Promise<PasteSelection> {
  return Excel.run(async (context) => {
    const range = context.workbook.getSelectedRange();
    range.load("values");
    const worksheet = range.worksheet;
    worksheet.load("name");
    await context.sync();

    const values = range.values as unknown[][];
    if (!values || values.length < 2) {
      throw new Error(
        "Select a table with a header row. Need to provide: client_alias, ticker, asset_class, currency and quantity for me to intake.",
      );
    }

    const headers = values[0].map(normalizeHeader);
    const idx = {
      client_alias: headers.indexOf("client_alias"),
      ticker: headers.indexOf("ticker"),
      quantity: headers.indexOf("quantity"),
      asset_class: headers.indexOf("asset_class"),
      currency:
        headers.indexOf("currency") >= 0
          ? headers.indexOf("currency")
          : headers.indexOf("ccy"),
    };
    if (
      idx.client_alias < 0 ||
      idx.ticker < 0 ||
      idx.quantity < 0 ||
      idx.asset_class < 0 ||
      idx.currency < 0
    ) {
      throw new Error(
        "Select a table with a header row. Need to provide: client_alias, ticker, asset_class, currency and quantity for me to intake.",
      );
    }

    const rows: PasteRow[] = [];
    for (let r = 1; r < values.length; r++) {
      const line = values[r];
      const clientAlias = String(line[idx.client_alias] ?? "").trim();
      const ticker = String(line[idx.ticker] ?? "").trim();
      const qtyRaw = line[idx.quantity];
      const quantity =
        qtyRaw === "" || qtyRaw === null || qtyRaw === undefined
          ? null
          : Number(qtyRaw);
      const assetClass = String(line[idx.asset_class] ?? "").trim();
      const currency =
        idx.currency >= 0
          ? String(line[idx.currency] ?? "").trim() || null
          : null;
      if (!clientAlias && !ticker && !assetClass) continue;
      rows.push({
        client_alias: clientAlias,
        ticker,
        quantity: quantity !== null && Number.isFinite(quantity) ? quantity : null,
        asset_class: assetClass,
        currency,
      });
    }
    if (!rows.length) throw new Error("No data rows found under the header.");
    return { rows, sourceSheetName: worksheet.name };
  });
}

async function readOrdBlotterRows(): Promise<OrderBlotterRow[]> {
  return Excel.run(async (context) => {
    const sheets = context.workbook.worksheets;
    sheets.load("items/name");
    await context.sync();
    const sheet = sheets.items.find((s) => s.name === ORD_BLOTTER_SHEET);
    if (!sheet) {
      throw new Error(
        `Sheet '${ORD_BLOTTER_SHEET}' missing — run optimization first.`,
      );
    }
    const used = sheet.getUsedRangeOrNullObject();
    used.load("values");
    await context.sync();
    if (used.isNullObject) {
      throw new Error(`Sheet '${ORD_BLOTTER_SHEET}' is empty.`);
    }
    const values = used.values as unknown[][];
    let headerIdx = -1;
    for (let i = 0; i < values.length; i++) {
      const headers = (values[i] || []).map(normalizeHeader);
      if (headers.includes("order_id") && headers.includes("ticker")) {
        headerIdx = i;
        break;
      }
    }
    if (headerIdx < 0) {
      throw new Error(
        `Could not find order header row on '${ORD_BLOTTER_SHEET}'.`,
      );
    }
    const headers = (values[headerIdx] || []).map(normalizeHeader);
    const idx = {
      order_id: headers.indexOf("order_id"),
      client_alias: headers.indexOf("client_alias"),
      status: headers.indexOf("status"),
      ticker: headers.indexOf("ticker"),
      asset_class: headers.indexOf("asset_class"),
      side: headers.indexOf("side"),
      quantity: headers.indexOf("quantity"),
      exec_type: headers.indexOf("exec_type"),
    };
    if (idx.order_id < 0 || idx.ticker < 0 || idx.side < 0 || idx.quantity < 0) {
      throw new Error(
        "ord_blotter header must include order_id, ticker, side, quantity",
      );
    }
    const out: OrderBlotterRow[] = [];
    for (let r = headerIdx + 1; r < values.length; r++) {
      const line = values[r] || [];
      const orderId = String(line[idx.order_id] ?? "").trim();
      const ticker = String(line[idx.ticker] ?? "").trim();
      if (!orderId && !ticker) continue;
      const qtyRaw = line[idx.quantity];
      const quantity =
        qtyRaw === "" || qtyRaw === null || qtyRaw === undefined
          ? null
          : Number(qtyRaw);
      out.push({
        order_id: orderId,
        client_alias:
          idx.client_alias >= 0
            ? String(line[idx.client_alias] ?? "").trim()
            : undefined,
        status:
          idx.status >= 0 ? String(line[idx.status] ?? "").trim() : undefined,
        ticker,
        asset_class:
          idx.asset_class >= 0
            ? String(line[idx.asset_class] ?? "").trim()
            : undefined,
        side: String(line[idx.side] ?? "").trim(),
        quantity:
          quantity !== null && Number.isFinite(quantity) ? quantity : null,
        exec_type:
          idx.exec_type >= 0
            ? String(line[idx.exec_type] ?? "").trim()
            : undefined,
      });
    }
    return out;
  });
}

async function applyExcelWrite(spec: ExcelWriteSpec): Promise<void> {
  const sheetName = spec.sheet_name?.trim();
  if (!sheetName) return;
  const values = spec.values ?? [];
  const activate = spec.activate !== false;
  const formats = spec.column_number_formats ?? {};
  const hideSource = spec.hide_source_sheet?.trim() || null;

  await Excel.run(async (context) => {
    const sheets = context.workbook.worksheets;
    sheets.load("items/name,items/visibility");
    await context.sync();

    let sheet = sheets.items.find((s) => s.name === sheetName);
    if (!sheet) {
      sheet = sheets.add(sheetName);
    }
    sheet.visibility = Excel.SheetVisibility.visible;
    if (activate) sheet.activate();

    const used = sheet.getUsedRangeOrNullObject();
    used.load("address");
    await context.sync();
    if (!used.isNullObject) used.clear();

    if (values.length > 0) {
      const width = Math.max(...values.map((row) => row.length), 1);
      const padded = values.map((row) => {
        const copy = [...row];
        while (copy.length < width) copy.push("");
        return copy;
      });
      const range = sheet.getRangeByIndexes(0, 0, padded.length, width);
      range.values = padded;

      if (padded.length > 1 && Object.keys(formats).length > 0) {
        const headers = padded[0].map((h) => String(h ?? ""));
        const dataRows = padded.length - 1;
        for (const [headerName, numFmt] of Object.entries(formats)) {
          const col = headers.findIndex(
            (h) => h === headerName || normalizeHeader(h) === normalizeHeader(headerName),
          );
          if (col < 0) continue;
          sheet.getRangeByIndexes(1, col, dataRows, 1).numberFormat = Array.from(
            { length: dataRows },
            () => [numFmt],
          );
        }
      }
      range.format.autofitColumns();
    }

    if (hideSource && hideSource !== sheetName) {
      sheets.load("items/name,items/visibility");
      await context.sync();
      const source = sheets.items.find((s) => s.name === hideSource);
      const visibleCount = sheets.items.filter(
        (s) => s.visibility === Excel.SheetVisibility.visible,
      ).length;
      if (source) {
        const sourceUsed = source.getUsedRangeOrNullObject();
        sourceUsed.load("address");
        await context.sync();
        if (!sourceUsed.isNullObject) sourceUsed.clear();
        if (
          source.visibility === Excel.SheetVisibility.visible &&
          visibleCount > 1
        ) {
          source.visibility = Excel.SheetVisibility.hidden;
        }
      }
    }

    await context.sync();
  });
}

async function applyExcelWrites(writes: ExcelWriteSpec[]): Promise<void> {
  for (const spec of writes) {
    await applyExcelWrite(spec);
  }
}

function triggerDownload(download: DownloadArtifact): void {
  try {
    const binary = atob(download.content_base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const blob = new Blob([bytes], { type: "application/pdf" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = sanitize(download.filename) || "download.pdf";
    anchor.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    setStatus(
      err instanceof Error ? err.message : "Failed to save download",
      true,
    );
  }
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function postCopilot(
  path: string,
  body: unknown,
  timeoutMs: number,
  correlationId: string,
): Promise<{ ok: boolean; status: number; text: string }> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: authHeaders(correlationId),
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await res.text();
    return { ok: res.ok, status: res.status, text };
  } finally {
    window.clearTimeout(timer);
  }
}

async function handleSuccessfulResponse(
  resp: CopilotAgentResponse,
  correlationId: string,
): Promise<void> {
  applyResponseUi(resp);
  const writes = resp.excel_writes ?? [];
  if (writes.length > 0) {
    setStatus(`Applying ${writes.length} Excel write(s)…`);
    try {
      await applyExcelWrites(writes);
      setStatus("Excel updated");
    } catch (err) {
      setStatus(
        `${err instanceof Error ? err.message : "Excel write failed"}\nRef: ${ErrorCode.CLIENT} · ${shortRef(correlationId)}`,
        true,
      );
    }
  } else {
    setStatus("");
  }
  if (resp.download?.content_base64) {
    triggerDownload(resp.download);
  }
}

async function createSession(): Promise<void> {
  const correlationId = newCorrelationId();
  setBusy(true, "Starting…");
  setStatus("Creating session…");
  try {
    const res = await postCopilot(
      "/api/v1/copilot/sessions",
      {},
      DEFAULT_TIMEOUT_MS,
      correlationId,
    );
    if (!res.ok) {
      setStatus(
        formatApiError(res.status, res.text, ErrorCode.SERVER, correlationId),
        true,
      );
      renderBadges([]);
      return;
    }
    let resp: CopilotAgentResponse;
    try {
      resp = JSON.parse(res.text) as CopilotAgentResponse;
    } catch {
      setStatus(supportMessage(ErrorCode.PARSE, correlationId), true);
      return;
    }
    await handleSuccessfulResponse(resp, correlationId);
  } catch (err) {
    if (isAbortError(err)) {
      setStatus(supportMessage(ErrorCode.TIMEOUT, correlationId), true);
    } else {
      setStatus(supportMessage(ErrorCode.NETWORK, correlationId), true);
    }
    renderBadges([]);
  } finally {
    setBusy(false);
    if (lastActions.length) renderActions(lastActions);
  }
}

async function handleAction(action: ActionTrigger): Promise<void> {
  if (busy || !sessionId) return;
  const correlationId = newCorrelationId();
  const timeoutMs = timeoutForAction(action.id);
  const busyLabel =
    action.id === "confirm_scenario_params" || action.id === "run_optimization"
      ? "Optimizing…"
      : "Working…";
  setBusy(true, busyLabel);
  setStatus(
    action.id === "confirm_scenario_params"
      ? "Confirming & running optimization…"
      : `Running: ${action.label}…`,
  );

  try {
    let rows: PasteRow[] | undefined;
    let sourceSheetName: string | undefined;
    let orderRows: OrderBlotterRow[] | undefined;
    const needsSelection =
      Boolean(action.requires_excel_selection) ||
      SELECTION_ACTION_IDS.has(action.id);

    if (needsSelection) {
      setStatus("Reading Excel selection…");
      const selection = await readSelectedPasteRows();
      rows = selection.rows;
      sourceSheetName = selection.sourceSheetName;
    }

    if (ORD_BLOTTER_ACTION_IDS.has(action.id)) {
      setStatus("Reading ord_blotter…");
      orderRows = await readOrdBlotterRows();
    }

    const payload: {
      action_id: string;
      rows?: PasteRow[];
      source_sheet_name?: string;
      order_rows?: OrderBlotterRow[];
    } = { action_id: action.id };
    if (rows) payload.rows = rows;
    if (sourceSheetName) payload.source_sheet_name = sourceSheetName;
    if (orderRows) payload.order_rows = orderRows;

    const res = await postCopilot(
      `/api/v1/copilot/sessions/${sessionId}/actions`,
      payload,
      timeoutMs,
      correlationId,
    );
    if (!res.ok) {
      setStatus(
        formatApiError(res.status, res.text, ErrorCode.SERVER, correlationId),
        true,
      );
      renderBadges([]);
      return;
    }
    let resp: CopilotAgentResponse;
    try {
      resp = JSON.parse(res.text) as CopilotAgentResponse;
    } catch {
      setStatus(supportMessage(ErrorCode.PARSE, correlationId), true);
      return;
    }
    await handleSuccessfulResponse(resp, correlationId);
  } catch (err) {
    if (isAbortError(err)) {
      setStatus(supportMessage(ErrorCode.TIMEOUT, correlationId), true);
    } else {
      setStatus(
        `${err instanceof Error ? err.message : "Action failed"}\nRef: ${ErrorCode.CLIENT} · ${shortRef(correlationId)}`,
        true,
      );
    }
    renderBadges([]);
  } finally {
    setBusy(false);
    if (lastActions.length) renderActions(lastActions);
  }
}

async function sendMessage(): Promise<void> {
  if (busy || !sessionId || !els.userText) return;
  const text = els.userText.value.trim();
  if (!text) {
    setStatus("Enter a message.", true);
    return;
  }
  if (text.length > 1000) {
    setStatus("Message must be at most 1000 characters.", true);
    return;
  }

  const correlationId = newCorrelationId();
  appendBubble("user", text);
  els.userText.value = "";
  setBusy(true, "Working…");
  setStatus("Sending…");

  // Free-text may be scenario parse — use longer timeout.
  const timeoutMs = SCENARIO_TIMEOUT_MS;
  try {
    const res = await postCopilot(
      `/api/v1/copilot/sessions/${sessionId}/messages`,
      { text },
      timeoutMs,
      correlationId,
    );
    if (!res.ok) {
      setStatus(
        formatApiError(res.status, res.text, ErrorCode.SERVER, correlationId),
        true,
      );
      renderBadges([]);
      return;
    }
    let resp: CopilotAgentResponse;
    try {
      resp = JSON.parse(res.text) as CopilotAgentResponse;
    } catch {
      setStatus(supportMessage(ErrorCode.PARSE, correlationId), true);
      return;
    }
    await handleSuccessfulResponse(resp, correlationId);
  } catch (err) {
    if (isAbortError(err)) {
      setStatus(supportMessage(ErrorCode.TIMEOUT, correlationId), true);
    } else {
      setStatus(supportMessage(ErrorCode.NETWORK, correlationId), true);
    }
    renderBadges([]);
  } finally {
    setBusy(false);
    if (lastActions.length) renderActions(lastActions);
  }
}

// ---------------------------------------------------------------------------
// Entry
// ---------------------------------------------------------------------------

export function initCopilot(): void {
  clearLegacyScenarioKeys();
  wirePilotAuthPanel();

  els.sendBtn?.addEventListener("click", () => {
    void sendMessage();
  });
  els.userText?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      void sendMessage();
    }
  });

  setStatus("Connecting…");
  void createSession();
}
