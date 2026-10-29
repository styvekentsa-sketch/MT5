import type {
  Account,
  AccountsList,
  Candles,
  ClosedPosition,
  ControlResult,
  DataFeeds,
  KillSwitchResult,
  ModeResult,
  Position,
  Sentiment,
  Status,
  WebhookLog,
} from "./types";

/** Paramètres de connexion à l'API du bot (modifiables dans l'application). */
export interface Connection {
  baseUrl: string;
  apiKey: string;
}

const API_PORT = 8000;
const DEFAULT_TIMEOUT_MS = 8_000;
// Le premier appel peut télécharger tous les flux RSS côté serveur
const SENTIMENT_TIMEOUT_MS = 60_000;
const SENTIMENT_ARTICLE_LIMIT = 30;
const DATA_FEED_ARTICLE_LIMIT = 100;

/** Supprime les « / » finaux ; retourne "" si l'URL n'est pas en http(s). */
export function normalizeBaseUrl(url: string): string {
  const trimmed = url.trim().replace(/\/+$/, "");
  try {
    const { protocol } = new URL(trimmed);
    return protocol === "http:" || protocol === "https:" ? trimmed : "";
  } catch {
    return "";
  }
}

/**
 * URL par défaut : VITE_API_BASE_URL (build), sinon même hôte que la page sur le port 8000.
 * Un téléphone qui ouvre http://192.168.1.20:5173 vise ainsi http://192.168.1.20:8000.
 */
export const DEFAULT_API_URL =
  normalizeBaseUrl(import.meta.env.VITE_API_BASE_URL ?? import.meta.env.VITE_API_URL ?? "") ||
  `${window.location.protocol}//${window.location.hostname}:${API_PORT}`;

/** Une page HTTPS ne peut pas appeler une API en HTTP (contenu mixte bloqué par le navigateur). */
export function isMixedContent(baseUrl: string): boolean {
  return window.location.protocol === "https:" && baseUrl.startsWith("http:");
}

/**
 * network      : API injoignable (bot arrêté, mauvaise URL, délai dépassé, hors ligne)
 * unauthorized : clé API absente ou invalide (401)
 * unavailable  : API joignable mais MT5 ou une donnée indisponible (503)
 * http         : autre erreur HTTP
 */
export type ApiErrorKind = "network" | "unauthorized" | "unavailable" | "http";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status?: number;

  constructor(message: string, kind: ApiErrorKind, status?: number) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
  }
}

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
}

async function readErrorDetail(response: Response): Promise<string> {
  try {
    const payload = await response.json();
    const detail = payload?.detail;
    if (typeof detail === "string") return detail;
    // Erreurs de validation FastAPI : [{ loc, msg, type }, ...]
    if (Array.isArray(detail)) return detail.map((d) => d?.msg ?? String(d)).join(" ; ");
  } catch {
    // corps non JSON : on retombe sur le statut HTTP
  }
  return `${response.status} ${response.statusText}`.trim();
}

function errorKind(status: number): ApiErrorKind {
  if (status === 401) return "unauthorized";
  if (status === 503) return "unavailable";
  return "http";
}

function networkErrorMessage(baseUrl: string, timedOut: boolean): string {
  if (timedOut) return "Délai de réponse dépassé";
  if (!navigator.onLine) return "Appareil hors ligne";
  if (isMixedContent(baseUrl)) return `Page en HTTPS : le navigateur bloque l'API en HTTP (${baseUrl})`;
  return `API déconnectée : impossible de joindre ${baseUrl}`;
}

async function request<T>(conn: Connection, path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, signal, timeoutMs = DEFAULT_TIMEOUT_MS } = options;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const onExternalAbort = () => controller.abort();
  signal?.addEventListener("abort", onExternalAbort, { once: true });

  let response: Response;
  try {
    response = await fetch(`${conn.baseUrl}${path}`, {
      method,
      headers: {
        "X-API-Key": conn.apiKey,
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
      cache: "no-store",
    });
  } catch (error) {
    if (signal?.aborted) throw error; // annulation volontaire (démontage du composant)
    throw new ApiError(networkErrorMessage(conn.baseUrl, controller.signal.aborted), "network");
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener("abort", onExternalAbort);
  }

  if (!response.ok) {
    throw new ApiError(await readErrorDetail(response), errorKind(response.status), response.status);
  }
  return (await response.json()) as T;
}

/**
 * Connexion vers un compte précis via la passerelle multi-comptes : toutes les routes
 * habituelles (/api/status, /api/positions...) deviennent /api/accounts/{id}/api/...
 */
export function scopedConnection(connection: Connection, accountId: string): Connection {
  return { ...connection, baseUrl: `${connection.baseUrl}/api/accounts/${encodeURIComponent(accountId)}` };
}

export const api = {
  /** Passerelle multi-comptes uniquement (404 en mode compte unique) */
  accounts: (c: Connection, signal?: AbortSignal) => request<AccountsList>(c, "/api/accounts", { signal }),
  status: (c: Connection, signal?: AbortSignal) => request<Status>(c, "/api/status", { signal }),
  account: (c: Connection, signal?: AbortSignal) => request<Account>(c, "/api/account", { signal }),
  positions: (c: Connection, signal?: AbortSignal) => request<Position[]>(c, "/api/positions", { signal }),
  sentiment: (c: Connection, signal?: AbortSignal) =>
    request<Sentiment>(c, `/api/sentiment?limit=${SENTIMENT_ARTICLE_LIMIT}`, {
      signal,
      timeoutMs: SENTIMENT_TIMEOUT_MS,
    }),
  dataFeeds: (c: Connection, signal?: AbortSignal) =>
    request<DataFeeds>(c, `/api/data-feeds?limit=${DATA_FEED_ARTICLE_LIMIT}`, {
      signal,
      timeoutMs: SENTIMENT_TIMEOUT_MS,
    }),
  webhookEvents: (c: Connection, signal?: AbortSignal) => request<WebhookLog>(c, "/api/webhooks/events", { signal }),
  candles: (c: Connection, symbol: string, timeframe: string, count: number, signal?: AbortSignal) =>
    request<Candles>(
      c,
      `/api/market/candles?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&count=${count}`,
      { signal },
    ),

  start: (c: Connection) => request<ControlResult>(c, "/api/control/start", { method: "POST" }),
  stop: (c: Connection) => request<ControlResult>(c, "/api/control/stop", { method: "POST" }),
  killSwitch: (c: Connection, includeManual: boolean) =>
    request<KillSwitchResult>(c, "/api/control/kill-switch", {
      method: "POST",
      body: { include_manual: includeManual, pause_engine: true },
      timeoutMs: 30_000,
    }),
  setMode: (c: Connection, live: boolean) =>
    request<ModeResult>(c, "/api/control/mode", {
      method: "POST",
      body: live ? { dry_run: false, confirm: "LIVE" } : { dry_run: true },
    }),
  closePosition: (c: Connection, ticket: number) =>
    request<ClosedPosition>(c, `/api/control/positions/${ticket}/close`, { method: "POST" }),
};

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}
