/**
 * Excel WF-02 (legacy button UI): prompt → scenario_params sheet → Run →
 * rebalanced_portfolio + ord_blotter → PDF.
 *
 * Deprecated for the task pane: Copilot chat (`copilot.ts`) owns the workflow.
 * Kept for reference / optional Excel helpers; not imported by `taskpane.ts`.
 */

import DOMPurify from "dompurify";

const PARAMS_SHEET = "scenario_params";
/** Legacy name from early WF-02; still readable so old workbooks keep working. */
const LEGACY_PARAMS_SHEET = "Scenario";
const REBALANCED_SHEET = "rebalanced_portfolio";
const ORD_BLOTTER_SHEET = "ord_blotter";
/** Legacy results sheet name. */
const LEGACY_TGT_SHEET = "tgt_folio";
const PORTFOLIO_ID_KEY = "wbp_portfolio_id";
const RUN_ID_KEY = "wbp_scenario_run_id";
const PARAMS_KEY = "wbp_scenario_params";

const PARSE_TIMEOUT_MS = 120_000;
const PARAMS_TIMEOUT_MS = 15_000;
const RUN_TIMEOUT_MS = 120_000;
const PDF_TIMEOUT_MS = 30_000;

const ScenarioError = {
  TIMEOUT: "WBP-SCENARIO-TIMEOUT",
  PARSE: "WBP-SCENARIO-PARSE",
  PARAMS: "WBP-SCENARIO-PARAMS",
  RUN: "WBP-SCENARIO-RUN",
  PDF: "WBP-SCENARIO-PDF",
  SERVER: "WBP-SCENARIO-SERVER",
  NETWORK: "WBP-SCENARIO-NETWORK",
  CLIENT: "WBP-SCENARIO-CLIENT",
} as const;

const SUPPORT_MESSAGE =
  "System error, please try again later and raise to support.";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";

type ScenarioParams = {
  message_type: "scenario_params";
  workflow_id?: string | null;
  source_agent?: string;
  user_prompt: string;
  /** Optional; firm dial is configs.business_guardrails (Streamlit), not Excel. */
  coin_temperature?: number | null;
  /** Mean-variance λ; omit/blank → backend default 2.5 */
  risk_aversion?: number | null;
  /** Tickers locked to current BASE weight */
  freeze_list?: string[];
  /** Target weights as fractions 0–1 (sheet displays percent) */
  weight_overrides?: Record<string, number>;
  scenario: {
    scenario_name: string;
    scenario_type: string;
    severity: "low" | "medium" | "high";
    tone: "risk_off" | "risk_on" | "neutral";
    base_currency: "USD";
    investment_horizon: string;
    lookback_window: string;
    data_frequency: string;
  };
  detected_factors: {
    macro_factors: string[];
    explicit_market_levels: Record<string, unknown>;
    impacted_asset_classes: string[];
  };
  market_data_request_params: {
    lookback_window: string;
    frequency: string;
    required_fields: string[];
    include_cash_proxy: boolean;
  };
  research_context_tags: string[];
  downstream_instructions: {
    for_market_data_service: string;
    for_research_agents: string;
    for_quant_agent: string;
  };
  confidence: {
    scenario_classification_confidence: number;
    horizon_selection_confidence: number;
  };
};

type Metrics = {
  expected_return?: number | null;
  annualized_volatility?: number | null;
  sharpe_ratio?: number | null;
  sortino_ratio?: number | null;
  max_drawdown?: number | null;
  max_runup?: number | null;
  cvar_95?: number | null;
  scenario_return?: number | null;
};

type OptiPosition = {
  ticker: string;
  asset_class: string;
  quantity: number;
  weight_pct: number;
  notional_value?: number | null;
  spot_price?: number | null;
  currency?: string;
};

type StagedTrade = {
  order_id?: string;
  trade_ref?: string; // legacy
  client_alias?: string | null;
  status?: string;
  ticker: string;
  asset_class: string;
  side: string;
  quantity: number;
  exec_type?: string;
  notional_value?: number | null;
  from_weight_pct?: number | null;
  to_weight_pct?: number | null;
};

type RunBody = {
  status: string;
  error_code?: string | null;
  error_message?: string | null;
  messages?: string[];
  opti_folio?: OptiPosition[];
  base_client_metrics?: Metrics | null;
  opti_client_metrics?: Metrics | null;
  metrics_delta?: Record<string, number | null>;
  staged_trades?: StagedTrade[];
  talking_points?: { bullet: string; driver?: string | null }[];
};

const EDITABLE_FIELDS = [
  "scenario_name",
  "scenario_type",
  "investment_horizon",
  "lookback_window",
  "risk_aversion",
  "freeze_list",
  "weight_overrides",
  "macro_factors",
  "research_context_tags",
  "user_prompt",
] as const;

function sanitize(text: string): string {
  return DOMPurify.sanitize(text, { ALLOWED_TAGS: [], ALLOWED_ATTR: [] });
}

function shortRef(correlationId: string): string {
  return correlationId.replace(/-/g, "").slice(0, 8);
}

function newCorrelationId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `local-${Date.now()}`;
}

function supportMessage(errorCode: string, correlationId: string): string {
  return `${SUPPORT_MESSAGE}\nRef: ${errorCode} · ${shortRef(correlationId)}`;
}

function authHeaders(correlationId: string): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Request-ID": correlationId,
  };
  const token = localStorage.getItem("sb_access_token");
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

function formatApiError(
  httpStatus: number,
  text: string,
  fallbackCode: string,
  correlationId: string,
): string {
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    const d = parsed.detail;
    if (d && typeof d === "object" && !Array.isArray(d)) {
      const body = d as {
        error_code?: string;
        message?: string;
        correlation_id?: string;
      };
      const code = body.error_code || fallbackCode;
      const cid = body.correlation_id || correlationId;
      if (httpStatus >= 500 || code === ScenarioError.SERVER) {
        return supportMessage(code, cid);
      }
      return `${body.message || "Request failed"}\nRef: ${code} · ${shortRef(cid)}`;
    }
    if (typeof d === "string") {
      return `${d}\nRef: ${fallbackCode} · ${shortRef(correlationId)}`;
    }
  } catch {
    // raw text
  }
  if (httpStatus >= 500) return supportMessage(fallbackCode, correlationId);
  return `${text || `Request failed (${httpStatus})`}\nRef: ${fallbackCode} · ${shortRef(correlationId)}`;
}

function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

function loadParams(): ScenarioParams | null {
  const raw = localStorage.getItem(PARAMS_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as ScenarioParams;
  } catch {
    return null;
  }
}

function saveParams(params: ScenarioParams, runId: string): void {
  localStorage.setItem(PARAMS_KEY, JSON.stringify(params));
  localStorage.setItem(RUN_ID_KEY, runId);
}

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function formatWeightOverrides(overrides: Record<string, number> | undefined): string {
  if (!overrides || !Object.keys(overrides).length) return "";
  return Object.entries(overrides)
    .map(([t, w]) => {
      const frac = Number(w);
      const pct = frac > 0 && frac <= 1 ? frac * 100 : frac;
      const pretty = Number.isInteger(pct) ? String(pct) : pct.toFixed(2);
      return `${t}=${pretty}`;
    })
    .join(", ");
}

function parseWeightOverrides(raw: string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const part of raw.split(",")) {
    const token = part.trim();
    if (!token) continue;
    const eq = token.indexOf("=");
    if (eq <= 0) {
      throw new Error(
        "weight_overrides format: TICKER=pct (e.g. AAPL=15, MSFT=10) — values are percent",
      );
    }
    const ticker = token.slice(0, eq).trim().toUpperCase();
    const pct = Number(token.slice(eq + 1).trim());
    if (!ticker || !Number.isFinite(pct) || pct < 0 || pct > 100) {
      throw new Error(`weight_overrides: invalid entry '${token}' (use 0–100 percent)`);
    }
    out[ticker] = pct / 100;
  }
  return out;
}

function fieldMap(params: ScenarioParams): Record<string, string> {
  return {
    scenario_name: params.scenario.scenario_name,
    scenario_type: params.scenario.scenario_type,
    investment_horizon: params.scenario.investment_horizon,
    lookback_window: params.scenario.lookback_window,
    risk_aversion:
      params.risk_aversion != null && Number.isFinite(params.risk_aversion)
        ? String(params.risk_aversion)
        : "2.5",
    freeze_list: (params.freeze_list || []).join(", "),
    weight_overrides: formatWeightOverrides(params.weight_overrides),
    macro_factors: (params.detected_factors.macro_factors || []).join(", "),
    research_context_tags: (params.research_context_tags || []).join(", "),
    user_prompt: params.user_prompt,
  };
}

function applyFieldEdits(
  base: ScenarioParams,
  edits: Record<string, string>,
): ScenarioParams {
  const next: ScenarioParams = structuredClone(base);
  const name = edits.scenario_name?.trim();
  const type = edits.scenario_type?.trim();
  const horizon = edits.investment_horizon?.trim();
  const lookback = edits.lookback_window?.trim();
  const riskRaw = edits.risk_aversion?.trim();
  if (name) next.scenario.scenario_name = name;
  if (type) next.scenario.scenario_type = type;
  if (horizon) next.scenario.investment_horizon = horizon;
  if (lookback) {
    next.scenario.lookback_window = lookback;
    next.market_data_request_params.lookback_window = lookback;
  }
  if (riskRaw !== undefined) {
    if (!riskRaw) {
      next.risk_aversion = null;
    } else {
      const lam = Number(riskRaw);
      if (!Number.isFinite(lam) || lam <= 0) {
        throw new Error("risk_aversion must be a positive number (default 2.5)");
      }
      next.risk_aversion = lam;
    }
  }
  if (edits.freeze_list !== undefined) {
    next.freeze_list = splitList(edits.freeze_list).map((t) => t.toUpperCase());
  }
  if (edits.weight_overrides !== undefined) {
    next.weight_overrides = parseWeightOverrides(edits.weight_overrides);
  }
  if (edits.macro_factors !== undefined) {
    next.detected_factors.macro_factors = splitList(edits.macro_factors);
  }
  if (edits.research_context_tags !== undefined) {
    next.research_context_tags = splitList(edits.research_context_tags);
  }
  if (edits.user_prompt?.trim()) next.user_prompt = edits.user_prompt.trim();
  // Strip any legacy Excel coin_temperature — management dial is config-only.
  delete next.coin_temperature;
  return next;
}

async function getOrAddSheet(
  context: Excel.RequestContext,
  name: string,
): Promise<Excel.Worksheet> {
  const sheets = context.workbook.worksheets;
  sheets.load("items/name");
  await context.sync();
  let sheet = sheets.items.find((s) => s.name === name);
  if (!sheet) sheet = sheets.add(name);
  sheet.visibility = Excel.SheetVisibility.visible;
  sheet.activate();
  return sheet;
}

async function clearUsed(sheet: Excel.Worksheet, context: Excel.RequestContext): Promise<void> {
  const used = sheet.getUsedRangeOrNullObject();
  used.load("address");
  await context.sync();
  if (!used.isNullObject) used.clear();
}

async function findSheet(
  context: Excel.RequestContext,
  names: string[],
): Promise<Excel.Worksheet | undefined> {
  const sheets = context.workbook.worksheets;
  sheets.load("items/name");
  await context.sync();
  for (const name of names) {
    const hit = sheets.items.find((s) => s.name === name);
    if (hit) return hit;
  }
  return undefined;
}

async function writeParamsSheet(
  params: ScenarioParams,
  scenarioRunId: string,
): Promise<void> {
  const map = fieldMap(params);
  await Excel.run(async (context) => {
    // Prefer new name; rename legacy "Scenario" sheet if present.
    let sheet = await findSheet(context, [PARAMS_SHEET, LEGACY_PARAMS_SHEET]);
    if (!sheet) {
      sheet = await getOrAddSheet(context, PARAMS_SHEET);
    } else if (sheet.name !== PARAMS_SHEET) {
      sheet.name = PARAMS_SHEET;
    }
    sheet.visibility = Excel.SheetVisibility.visible;
    sheet.activate();
    await clearUsed(sheet, context);
    const values: string[][] = [["field", "value"]];
    for (const field of EDITABLE_FIELDS) {
      values.push([field, map[field] ?? ""]);
    }
    const range = sheet.getRangeByIndexes(0, 0, values.length, 2);
    range.values = values;
    range.format.autofitColumns();
    await context.sync();
  });
}

async function readParamsEdits(): Promise<Record<string, string>> {
  return Excel.run(async (context) => {
    const sheet = await findSheet(context, [PARAMS_SHEET, LEGACY_PARAMS_SHEET]);
    if (!sheet) {
      throw new Error("scenario_params sheet missing — Parse scenario first.");
    }
    const used = sheet.getUsedRangeOrNullObject();
    used.load("values");
    await context.sync();
    if (used.isNullObject) throw new Error("scenario_params sheet is empty.");
    const values = used.values as unknown[][];
    const edits: Record<string, string> = {};
    for (let i = 1; i < values.length; i++) {
      const field = String(values[i][0] ?? "").trim();
      const value = String(values[i][1] ?? "");
      if (field) edits[field] = value;
    }
    return edits;
  });
}

function fmt(n: number | null | undefined): string | number {
  if (n === null || n === undefined || Number.isNaN(n)) return "";
  return Number(n.toFixed(4));
}

/** Equity: whole shares. Crypto/cash: 2 dp. */
function qty(n: number | null | undefined, assetClass?: string): string | number {
  if (n === null || n === undefined || Number.isNaN(n)) return "";
  const ac = (assetClass || "").toUpperCase();
  if (ac === "CASH" || ac === "CRYPTO") {
    return Number(Number(n).toFixed(2));
  }
  return Math.trunc(Number(n));
}

/** Match BASE scratchpad: nominal / money at 2 dp. */
function money(n: number | null | undefined): string | number {
  if (n === null || n === undefined || Number.isNaN(n)) return "";
  return Number(Number(n).toFixed(2));
}

/** Match BASE "Pct weight (USD)": fraction → percent, 2 dp. */
function pct(n: number | null | undefined): string | number {
  if (n === null || n === undefined || Number.isNaN(n)) return "";
  return Number((n * 100).toFixed(2));
}

function qtyNumberFormat(assetClass?: string): string {
  const ac = (assetClass || "").toUpperCase();
  if (ac === "CRYPTO" || ac === "CASH") return "#,##0.00";
  return "#,##0";
}

async function writeRebalancedPortfolio(
  body: RunBody,
  scenarioRunId: string,
): Promise<void> {
  const positions = body.opti_folio ?? [];
  const points = body.talking_points ?? [];
  const metricKeys: (keyof Metrics)[] = [
    "expected_return",
    "annualized_volatility",
    "sharpe_ratio",
    "sortino_ratio",
    "max_drawdown",
    "max_runup",
    "cvar_95",
    "scenario_return",
  ];

  const rows: (string | number)[][] = [];
  rows.push(["metrics", "base", "opti", "delta"]);
  for (const key of metricKeys) {
    rows.push([
      key,
      fmt(body.base_client_metrics?.[key]),
      fmt(body.opti_client_metrics?.[key]),
      fmt(body.metrics_delta?.[key]),
    ]);
  }
  rows.push([]);
  rows.push(["opti_folio"]);
  rows.push([
    "ticker",
    "asset_class",
    "currency",
    "quantity",
    "nominal_value",
    "Pct weight (USD)",
  ]);
  const optiStartRow = rows.length;
  for (const p of positions) {
    rows.push([
      p.ticker,
      p.asset_class,
      p.currency ?? "",
      qty(p.quantity, p.asset_class),
      money(p.notional_value),
      pct(p.weight_pct),
    ]);
  }
  const optiCount = positions.length;
  rows.push([]);
  rows.push(["talking_points"]);
  for (const tp of points) rows.push([tp.bullet]);

  const width = Math.max(...rows.map((r) => r.length), 1);
  const padded = rows.map((r) => {
    const copy = [...r];
    while (copy.length < width) copy.push("");
    return copy;
  });

  await Excel.run(async (context) => {
    let sheet = await findSheet(context, [REBALANCED_SHEET, LEGACY_TGT_SHEET]);
    if (!sheet) {
      sheet = await getOrAddSheet(context, REBALANCED_SHEET);
    } else if (sheet.name !== REBALANCED_SHEET) {
      sheet.name = REBALANCED_SHEET;
    }
    sheet.visibility = Excel.SheetVisibility.visible;
    sheet.activate();
    await clearUsed(sheet, context);
    const range = sheet.getRangeByIndexes(0, 0, padded.length, width);
    range.values = padded;
    if (optiCount > 0) {
      const qtyFormats = positions.map((p) => [qtyNumberFormat(p.asset_class)]);
      sheet.getRangeByIndexes(optiStartRow, 3, optiCount, 1).numberFormat = qtyFormats;
      sheet.getRangeByIndexes(optiStartRow, 4, optiCount, 1).numberFormat = Array.from(
        { length: optiCount },
        () => ["#,##0.00"],
      );
      sheet.getRangeByIndexes(optiStartRow, 5, optiCount, 1).numberFormat = Array.from(
        { length: optiCount },
        () => ["0.00"],
      );
    }
    range.format.autofitColumns();
    await context.sync();
  });
}

async function writeOrdBlotter(body: RunBody, scenarioRunId: string): Promise<void> {
  const trades = [...(body.staged_trades ?? [])].sort((a, b) => {
    const aw = a.to_weight_pct ?? -Infinity;
    const bw = b.to_weight_pct ?? -Infinity;
    if (bw !== aw) return bw - aw;
    return String(a.ticker).localeCompare(String(b.ticker));
  });
  const rows: (string | number)[][] = [
    [
      "order_id",
      "client_alias",
      "Status",
      "ticker",
      "asset_class",
      "side",
      "quantity",
      "exec_type",
      "from_Pct weight (USD)",
      "to_Pct weight (USD)",
    ],
  ];
  const tradesStartRow = rows.length;
  for (const t of trades) {
    rows.push([
      t.order_id || t.trade_ref || "",
      t.client_alias || "",
      t.status || "Pending",
      t.ticker,
      t.asset_class,
      t.side,
      qty(t.quantity, t.asset_class),
      t.exec_type || "Market",
      pct(t.from_weight_pct),
      pct(t.to_weight_pct),
    ]);
  }
  const tradesCount = trades.length;
  const width = Math.max(...rows.map((r) => r.length), 1);
  const padded = rows.map((r) => {
    const copy = [...r];
    while (copy.length < width) copy.push("");
    return copy;
  });

  await Excel.run(async (context) => {
    const sheet = await getOrAddSheet(context, ORD_BLOTTER_SHEET);
    await clearUsed(sheet, context);
    const range = sheet.getRangeByIndexes(0, 0, padded.length, width);
    range.values = padded;
    if (tradesCount > 0) {
      // quantity is column index 6 after new columns
      const tradeQtyFormats = trades.map((t) => [qtyNumberFormat(t.asset_class)]);
      sheet.getRangeByIndexes(tradesStartRow, 6, tradesCount, 1).numberFormat =
        tradeQtyFormats;
    }
    range.format.autofitColumns();
    await context.sync();
  });
}

async function postJson(
  path: string,
  method: string,
  body: unknown,
  timeoutMs: number,
  correlationId: string,
): Promise<{ ok: boolean; status: number; text: string }> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method,
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

export function rememberPortfolioId(portfolioId: string): void {
  if (portfolioId) localStorage.setItem(PORTFOLIO_ID_KEY, portfolioId);
}

export function initScenario(opts: {
  setStatus: (text: string) => void;
  render: (text: string) => void;
}): void {
  const portfolioInput = document.getElementById(
    "scenarioPortfolioId",
  ) as HTMLInputElement | null;
  const promptInput = document.getElementById(
    "scenarioPrompt",
  ) as HTMLTextAreaElement | null;
  const parseBtn = document.getElementById("parseScenario") as HTMLButtonElement | null;
  const updateBtn = document.getElementById("updateParams") as HTMLButtonElement | null;
  const runBtn = document.getElementById("runScenario") as HTMLButtonElement | null;
  const pdfBtn = document.getElementById("downloadPdf") as HTMLButtonElement | null;

  const storedPortfolio = localStorage.getItem(PORTFOLIO_ID_KEY);
  if (portfolioInput && storedPortfolio && !portfolioInput.value) {
    portfolioInput.value = storedPortfolio;
  }

  const buttons = [parseBtn, updateBtn, runBtn, pdfBtn].filter(
    (b): b is HTMLButtonElement => Boolean(b),
  );

  function setBusy(busy: boolean): void {
    for (const b of buttons) b.disabled = busy;
  }

  function portfolioId(): string {
    return (portfolioInput?.value || localStorage.getItem(PORTFOLIO_ID_KEY) || "").trim();
  }

  async function onParse(): Promise<void> {
    const correlationId = newCorrelationId();
    const pid = portfolioId();
    const prompt = (promptInput?.value || "").trim();
    if (!pid) {
      opts.render("Enter a portfolio id (run UPDATE PORTFOLIO first).");
      return;
    }
    if (!prompt) {
      opts.render("Enter a scenario prompt.");
      return;
    }
    setBusy(true);
    opts.setStatus("Parsing scenario…");
    try {
      const res = await postJson(
        "/api/v1/scenarios/parse",
        "POST",
        { portfolio_id: pid, user_prompt: prompt },
        PARSE_TIMEOUT_MS,
        correlationId,
      );
      if (!res.ok) {
        opts.render(
          formatApiError(res.status, res.text, ScenarioError.PARSE, correlationId),
        );
        opts.setStatus("Parse failed");
        return;
      }
      const parsed = JSON.parse(res.text) as {
        scenario_run_id: string;
        scenario_params: ScenarioParams;
        messages?: string[];
      };
      saveParams(parsed.scenario_params, parsed.scenario_run_id);
      localStorage.setItem(PORTFOLIO_ID_KEY, pid);
      await writeParamsSheet(parsed.scenario_params, parsed.scenario_run_id);
      opts.render(
        sanitize(
          [
            `scenario_run_id: ${parsed.scenario_run_id}`,
            "(also written to scenario_params sheet cell B1 — copy for SQL)",
            "Review scenario_params. Edit values, then Update parameters, or Run scenario to approve as-is.",
            ...(parsed.messages ?? []),
          ].join("\n"),
        ),
      );
      opts.setStatus("scenario_params sheet ready");
    } catch (err) {
      if (isAbortError(err)) {
        opts.render(supportMessage(ScenarioError.TIMEOUT, correlationId));
        opts.setStatus("Timed out");
        return;
      }
      opts.render(
        `${err instanceof Error ? err.message : "Parse failed"}\nRef: ${ScenarioError.CLIENT} · ${shortRef(correlationId)}`,
      );
      opts.setStatus("Error");
    } finally {
      setBusy(false);
    }
  }

  async function onUpdate(): Promise<void> {
    const correlationId = newCorrelationId();
    const runId = localStorage.getItem(RUN_ID_KEY);
    const base = loadParams();
    if (!runId || !base) {
      opts.render("Parse a scenario before updating parameters.");
      return;
    }
    setBusy(true);
    opts.setStatus("Updating parameters…");
    try {
      const edits = await readParamsEdits();
      const next = applyFieldEdits(base, edits);
      const res = await postJson(
        "/api/v1/scenarios/params",
        "PUT",
        { scenario_run_id: runId, scenario_params: next },
        PARAMS_TIMEOUT_MS,
        correlationId,
      );
      if (!res.ok) {
        opts.render(
          formatApiError(res.status, res.text, ScenarioError.PARAMS, correlationId),
        );
        opts.setStatus("Update failed");
        return;
      }
      const body = JSON.parse(res.text) as {
        is_exact_match: boolean;
        diffs: unknown[];
        scenario_params: ScenarioParams;
        scenario_params_version: number;
      };
      saveParams(body.scenario_params, runId);
      opts.render(
        sanitize(
          [
            `Parameters saved (version ${body.scenario_params_version}).`,
            body.is_exact_match
              ? "Exact match with agent parse (no edits)."
              : `Human edits: ${body.diffs.length} field(s).`,
          ].join("\n"),
        ),
      );
      opts.setStatus("Parameters updated");
    } catch (err) {
      if (isAbortError(err)) {
        opts.render(supportMessage(ScenarioError.TIMEOUT, correlationId));
        opts.setStatus("Timed out");
        return;
      }
      opts.render(
        `${err instanceof Error ? err.message : "Update failed"}\nRef: ${ScenarioError.CLIENT} · ${shortRef(correlationId)}`,
      );
      opts.setStatus("Error");
    } finally {
      setBusy(false);
    }
  }

  async function onRun(): Promise<void> {
    const correlationId = newCorrelationId();
    const runId = localStorage.getItem(RUN_ID_KEY);
    if (!runId) {
      opts.render("Parse a scenario before running.");
      return;
    }
    setBusy(true);
    opts.setStatus("Running scenario (up to 2 minutes)…");
    try {
      const res = await postJson(
        "/api/v1/scenarios/run",
        "POST",
        { scenario_run_id: runId },
        RUN_TIMEOUT_MS,
        correlationId,
      );
      if (!res.ok) {
        opts.render(formatApiError(res.status, res.text, ScenarioError.RUN, correlationId));
        opts.setStatus("Run failed");
        return;
      }
      const body = JSON.parse(res.text) as RunBody;
      if (body.status !== "COMPLETED") {
        opts.render(
          sanitize(
            `${body.error_message || "Scenario run failed"}\nRef: ${body.error_code || ScenarioError.RUN} · ${shortRef(correlationId)}`,
          ),
        );
        opts.setStatus("Run failed");
        return;
      }
      await writeRebalancedPortfolio(body, runId);
      await writeOrdBlotter(body, runId);
      const lines = [
        `scenario_run_id: ${runId}`,
        "rebalanced_portfolio updated.",
        `ord_blotter: ${(body.staged_trades ?? []).length} staged order(s)`,
        `Positions: ${(body.opti_folio ?? []).length}`,
        ...(body.messages ?? []),
      ];
      opts.render(sanitize(lines.join("\n")));
      opts.setStatus("Scenario complete — download PDF when ready");
    } catch (err) {
      if (isAbortError(err)) {
        opts.render(supportMessage(ScenarioError.TIMEOUT, correlationId));
        opts.setStatus("Timed out");
        return;
      }
      opts.render(
        `${err instanceof Error ? err.message : "Run failed"}\nRef: ${ScenarioError.CLIENT} · ${shortRef(correlationId)}`,
      );
      opts.setStatus("Error");
    } finally {
      setBusy(false);
    }
  }

  async function onPdf(): Promise<void> {
    const correlationId = newCorrelationId();
    const runId = localStorage.getItem(RUN_ID_KEY);
    if (!runId) {
      opts.render("Run a scenario before downloading the PDF.");
      return;
    }
    setBusy(true);
    opts.setStatus("Building PDF…");
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), PDF_TIMEOUT_MS);
    try {
      const res = await fetch(`${API_BASE}/api/v1/scenarios/pdf`, {
        method: "POST",
        headers: authHeaders(correlationId),
        body: JSON.stringify({ scenario_run_id: runId }),
        signal: controller.signal,
      });
      if (!res.ok) {
        const text = await res.text();
        opts.render(formatApiError(res.status, text, ScenarioError.PDF, correlationId));
        opts.setStatus("PDF failed");
        return;
      }
      const blob = await res.blob();
      const disposition = res.headers.get("Content-Disposition") || "";
      const match = /filename="([^"]+)"/.exec(disposition);
      const filename = match?.[1] || `scenario_${runId.slice(0, 8)}.pdf`;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      URL.revokeObjectURL(url);
      opts.render(sanitize(`PDF downloaded: ${filename}`));
      opts.setStatus("PDF ready");
    } catch (err) {
      if (isAbortError(err)) {
        opts.render(supportMessage(ScenarioError.TIMEOUT, correlationId));
        opts.setStatus("Timed out");
        return;
      }
      opts.render(
        `${err instanceof Error ? err.message : "PDF failed"}\nRef: ${ScenarioError.NETWORK} · ${shortRef(correlationId)}`,
      );
      opts.setStatus("Error");
    } finally {
      window.clearTimeout(timer);
      setBusy(false);
    }
  }

  parseBtn?.addEventListener("click", () => {
    void onParse();
  });
  updateBtn?.addEventListener("click", () => {
    void onUpdate();
  });
  runBtn?.addEventListener("click", () => {
    void onRun();
  });
  pdfBtn?.addEventListener("click", () => {
    void onPdf();
  });
  setBusy(false);
}
