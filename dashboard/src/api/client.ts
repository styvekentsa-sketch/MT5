import type {
  Account,
  ClosedPosition,
  ControlResult,
  KillSwitchResult,
  ModeResult,
  Position,
  Sentiment,
  Status,
} from "./types";

export const API_URL = (import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

const DEFAULT_TIMEOUT_MS = 8_000;
// Le premier appel peut télécharger tous les flux RSS côté serveur
const SENTIMENT_TIMEOUT_MS = 60_000;
const SENTIMENT_ARTICLE_LIMIT = 30;

/**
 * network      : API injoignable (bot arrêté, mauvaise URL, délai dépassé)
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

async function request<T>(path: string, apiKey: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, signal, timeoutMs = DEFAULT_TIMEOUT_MS } = options;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const onExternalAbort = () => controller.abort();
  signal?.addEventListener("abort", onExternalAbort, { once: true });

  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      method,
      headers: {
        "X-API-Key": apiKey,
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
  } catch (error) {
    if (signal?.aborted) throw error; // annulation volontaire (démontage du composant)
    const timedOut = controller.signal.aborted;
    throw new ApiError(
      timedOut ? "Délai de réponse dépassé" : `API déconnectée : impossible de joindre ${API_URL}`,
      "network",
    );
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener("abort", onExternalAbort);
  }

  if (!response.ok) {
    throw new ApiError(await readErrorDetail(response), errorKind(response.status), response.status);
  }
  return (await response.json()) as T;
}

export const api = {
  status: (key: string, signal?: AbortSignal) => request<Status>("/api/status", key, { signal }),
  account: (key: string, signal?: AbortSignal) => request<Account>("/api/account", key, { signal }),
  positions: (key: string, signal?: AbortSignal) => request<Position[]>("/api/positions", key, { signal }),
  sentiment: (key: string, signal?: AbortSignal) =>
    request<Sentiment>(`/api/sentiment?limit=${SENTIMENT_ARTICLE_LIMIT}`, key, {
      signal,
      timeoutMs: SENTIMENT_TIMEOUT_MS,
    }),

  start: (key: string) => request<ControlResult>("/api/control/start", key, { method: "POST" }),
  stop: (key: string) => request<ControlResult>("/api/control/stop", key, { method: "POST" }),
  killSwitch: (key: string, includeManual: boolean) =>
    request<KillSwitchResult>("/api/control/kill-switch", key, {
      method: "POST",
      body: { include_manual: includeManual, pause_engine: true },
      timeoutMs: 30_000,
    }),
  setMode: (key: string, live: boolean) =>
    request<ModeResult>("/api/control/mode", key, {
      method: "POST",
      body: live ? { dry_run: false, confirm: "LIVE" } : { dry_run: true },
    }),
  closePosition: (key: string, ticket: number) =>
    request<ClosedPosition>(`/api/control/positions/${ticket}/close`, key, { method: "POST" }),
};

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}
