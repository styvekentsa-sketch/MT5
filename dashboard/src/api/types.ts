// Miroir des modèles Pydantic de api/schemas.py (backend FastAPI)

export type Mode = "PAPER" | "LIVE";
export type EngineState = "running" | "paused" | "stopped";
export type SentimentLabel = "bullish" | "bearish" | "neutral";

export interface Outcome {
  symbol: string;
  status: string;
  detail: string;
}

export interface Status {
  state: EngineState;
  mode: Mode;
  mt5_connected: boolean;
  symbols: string[];
  timeframe: string;
  loop_interval_seconds: number;
  started_at: string | null;
  cycle_count: number;
  last_cycle_at: string | null;
  last_cycle_duration_seconds: number | null;
  last_outcomes: Outcome[];
}

export interface Account {
  login: number;
  currency: string;
  balance: number;
  equity: number;
  margin: number;
  margin_free: number;
  margin_level: number;
  leverage: number;
  floating_pl: number;
  realized_pl_today: number;
  daily_pl: number;
  daily_pl_pct: number;
}

export interface Position {
  ticket: number;
  symbol: string;
  type: "BUY" | "SELL";
  volume: number;
  price_open: number;
  price_current: number;
  sl: number | null;
  tp: number | null;
  profit: number;
  swap: number;
  opened_at: string;
  magic: number;
  is_bot: boolean;
  comment: string;
}

export interface Article {
  title: string;
  source: string;
  link: string;
  published: string | null;
  snippet: string;
  score: number;
  label: SentimentLabel;
}

export interface Sentiment {
  engine: string;
  global_score: number;
  global_label: SentimentLabel;
  by_symbol: Record<string, number>;
  article_count: number;
  articles: Article[];
}

export interface ControlResult {
  success: boolean;
  message: string;
  status: Status;
}

export interface ModeResult {
  mode: Mode;
  message: string;
}

export interface ClosedPosition {
  ticket: number;
  symbol: string;
  success: boolean;
  message: string;
}

export interface KillSwitchResult {
  closed: number;
  failed: number;
  engine_paused: boolean;
  results: ClosedPosition[];
}
