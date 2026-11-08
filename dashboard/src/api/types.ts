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
  /** Clé utilisée : complet (API_KEY) ou lecture seule (API_VIEWER_KEY) ; absent sur une ancienne version */
  access?: "complet" | "lecture";
  state: EngineState;
  mode: Mode;
  /** Connexion au broker active (MT5 ou exchange crypto ; nom historique) */
  mt5_connected: boolean;
  /** Plateforme : mt5, bybit, binanceusdm... (absent sur une ancienne version du bot) */
  broker?: string;
  broker_description?: string;
  market?: "forex" | "crypto";
  symbols: string[];
  timeframe: string;
  loop_interval_seconds: number;
  started_at: string | null;
  cycle_count: number;
  last_cycle_at: string | null;
  last_cycle_duration_seconds: number | null;
  last_outcomes: Outcome[];
  /** Trades d'opportunité actifs quand la stratégie principale dit HOLD (absent sur une ancienne version) */
  opportunities?: OpportunityMode[];
  /** Latence par étape (actualités, analyse, risque, ordre, tour), en ms */
  latency?: Record<string, LatencyStats>;
  /** Motif de suspension des nouvelles entrées (drawdown), null sinon */
  suspension?: string | null;
  /** Empreinte des règles appliquées et commit du code */
  rules_version?: string;
}

export interface LatencyStats {
  last_ms: number;
  avg_ms: number;
  p95_ms: number;
  max_ms: number;
  count: number;
}

export interface OpportunityMode {
  name: string;
  /** Impulsion, Petits trades */
  label: string;
  /** Ce que le mode cherche, son stop et son objectif */
  role: string;
  timeframe: string;
  /** Symboles autorisés (OPPORTUNITY_SYMBOLS) */
  symbols: string[];
}

export type AccountType = "demo" | "real" | "contest" | "paper" | "unknown";

export interface Account {
  /** Numéro de compte MT5 (0 pour un exchange crypto) */
  login: number;
  /** Identité du compte (absente sur une ancienne version du bot) */
  name?: string;
  company?: string;
  server?: string;
  account_type?: AccountType;
  broker?: string;
  market?: "forex" | "crypto";
  /** Libellé prêt à afficher : titulaire, sinon « Compte démo #login » */
  display_name?: string;
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
  /** Protection automatique (prix d'entrée à +1 R, stop suiveur dès +1,5 R) ; absente hors bot */
  protection?: PositionProtection | null;
}

export interface PositionProtection {
  stage: "initial" | "breakeven" | "trailing";
  initial_stop: number;
  /** R : distance entrée réelle - stop initial, figée à l'ouverture */
  risk: number;
  /** Meilleur Bid (achat) / Ask (vente) depuis l'ouverture */
  best: number;
  best_r: number;
  last_error: string;
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

// --- Données ingérées (GET /api/data-feeds) ---

export type FeedKind = "rss" | "calendar" | "webhook";
export type FeedStatus = "ok" | "error" | "pending" | "disabled";
export type CalendarStatus = "imminent" | "upcoming" | "passed";

export interface FeedSource {
  name: string;
  kind: FeedKind;
  url: string | null;
  status: FeedStatus;
  last_fetch: string | null;
  next_fetch: string | null;
  items: number;
  detail: string;
}

export interface NewsItem {
  title: string;
  source: string;
  link: string;
  published: string | null;
  fetched_at: string | null;
  snippet: string;
  score: number;
  label: SentimentLabel;
  keywords: string[];
  /** Symbole -> sens de l'impact (+1 base, -1 cotation, 1.5 paire citée) */
  symbols: Record<string, number>;
}

export interface CalendarEvent {
  title: string;
  currency: string;
  time: string;
  impact: string;
  forecast: string;
  previous: string;
  status: CalendarStatus;
  blocks_trading: boolean;
  symbols: string[];
}

export interface DataFeeds {
  generated_at: string;
  tracked_symbols: string[];
  block_window_minutes: [number, number];
  sources: FeedSource[];
  news: NewsItem[];
  calendar: CalendarEvent[];
}

// --- Webhooks entrants (GET /api/webhooks/events) ---

export interface WebhookEvent {
  id: number;
  received_at: string;
  source: string;
  symbol: string | null;
  action: string | null;
  accepted: boolean;
  reason: string;
  client: string;
  size_bytes: number;
  payload: unknown;
}

export interface WebhookLog {
  enabled: boolean;
  endpoint: string;
  capacity: number;
  events: WebhookEvent[];
}

// --- Marché (GET /api/market/candles) ---

export interface Candle {
  /** Ouverture de la bougie, secondes Unix */
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface Candles {
  symbol: string;
  timeframe: string;
  digits: number;
  bid: number | null;
  ask: number | null;
  candles: Candle[];
}

// --- Multi-comptes (passerelle : GET /api/accounts) ---

export interface AccountSummary {
  id: string;
  label: string;
  login: number;
  server: string;
  /** mt5, ou identifiant ccxt de l'exchange (bybit, binanceusdm...) */
  broker?: string;
  /** False si le processus du compte ne répond pas */
  reachable: boolean;
  error: string;
  state: EngineState | null;
  mode: Mode | null;
  mt5_connected: boolean | null;
  currency: string | null;
  balance: number | null;
  equity: number | null;
  daily_pl: number | null;
  daily_pl_pct: number | null;
  floating_pl: number | null;
  open_positions: number | null;
}

export interface AccountsList {
  accounts: AccountSummary[];
}

// --- Journal des décisions et rejets (GET /api/bot/decisions) ---

export type DecisionStage =
  | "signal"
  | "calendrier"
  | "cooldown"
  | "risque"
  | "broker"
  | "exécution"
  | "erreur"
  | "gestion"
  | "sécurité"
  | "mémoire"
  | "post-mortem";

/** Style choisi par le profil adaptive */
export type TradingMode = "scalping" | "day" | "swing";

export interface BotDecision {
  id: number;
  timestamp: string;
  /** Numéro du tour du moteur */
  cycle: number | null;
  symbol: string;
  /** Sens envisagé, même si le trade est refusé ; null si aucun signal */
  action: "ACHAT" | "VENTE" | null;
  /** ACTION : gestion d'une position (trailing, fin de session) ou coupure de sécurité */
  status: "ACCEPTÉ" | "REJETÉ" | "ACTION";
  stage: DecisionStage;
  mode?: TradingMode | null;
  /** Motif précis et chiffré */
  reason: string;
  /** Explication complète (technique, sentiment, filtres) */
  detail: string;
  confidence: number | null;
  technical_score: number | null;
  sentiment_score: number | null;
  hybrid_score: number | null;
  volume: number | null;
  ticket: number | null;
  dry_run: boolean | null;
}

export interface BotDecisions {
  source: "sqlite" | "memory";
  accepted: number;
  rejected: number;
  actions?: number;
  items: BotDecision[];
}

// --- Apprentissage : journal des trades, post-mortem, mémoire des erreurs (GET /api/learning) ---

export interface LearningTrade {
  id: number;
  timestamp: string;
  ticket: number;
  symbol: string;
  side: "BUY" | "SELL";
  volume: number;
  entry_price: number;
  sl: number | null;
  tp: number | null;
  mode: TradingMode | null;
  reason: string;
  features: Record<string, number | string | null>;
  pattern: string;
  status: "open" | "simulated" | "closed";
  net_profit?: number;
  close_reason?: string;
  duration_seconds?: number | null;
}

export interface LossAnalysis {
  id: number;
  analyzed_at: string;
  ticket: number;
  symbol: string;
  side: "BUY" | "SELL";
  mode: TradingMode | null;
  duration_seconds: number | null;
  net_profit: number;
  close_reason: string;
  adverse_excursion_pips: number | null;
  favorable_excursion_pips: number | null;
  sl_distance_pips: number;
  main_factor: string;
  factors: string[];
  pattern_description: string;
  entry_reason: string;
}

export interface MistakePattern {
  key: string;
  symbol: string;
  mode: TradingMode | null;
  side: "BUY" | "SELL";
  description: string;
  wins: number;
  losses: number;
  total_loss: number;
  main_factor?: string;
  loss_tickets: number[];
  last_loss_at?: string;
  blocking: boolean;
}

export interface LearningReport {
  enabled: boolean;
  trades_logged: number;
  open: number;
  simulated: number;
  closed: number;
  wins: number;
  losses: number;
  blocking_patterns: number;
  rules: { min_losses: number; min_loss_rate: number; memory_days: number };
  recent_trades: LearningTrade[];
  recent_losses: LossAnalysis[];
  patterns: MistakePattern[];
}

// --- Connexion dynamique à un compte MT5 (/api/auth/*) ---

export interface MT5Session {
  /** env = compte du .env, dashboard = choisi depuis l'interface */
  source: "env" | "dashboard";
  login: number | null;
  server: string | null;
  name: string;
  company: string;
  account_type: string;
  currency: string | null;
  balance: number | null;
  mode: Mode;
  engine_paused: boolean;
  /** Bouton « Algo Trading » du terminal ; false : MT5 refusera les ordres */
  algo_trading: boolean | null;
  message: string;
}

export interface MT5LoginRequest {
  login: number;
  password: string;
  server: string;
}

// --- Mode analyste (/api/analysis/*) ---

/** GET /api/analysis/risk : limites journalières */
export interface RiskLimits {
  /** Perte du jour en % du solde de début de journée (positive = perte) */
  daily_loss_pct: number | null;
  max_daily_loss_pct: number;
  daily_loss_reached: boolean;
  consecutive_losses: number;
  max_consecutive_losses: number;
  consecutive_losses_reached: boolean;
  risk_per_trade_pct: number;
  max_total_risk_pct: number;
  max_open_positions: number;
  deals_today: number;
  /** Baisse des fonds propres depuis leur plus haut, en % (absent sur une ancienne version) */
  drawdown_pct?: number | null;
  /** 0 = suspension désactivée */
  max_drawdown_pct?: number;
  drawdown_reached?: boolean;
  max_currency_risk_pct?: number;
  /** Risque au stop cumulé par devise et par sens (corrélation), en % du capital */
  currency_exposure?: { exposure: string; risk_pct: number | null }[];
}

export type SetupStatus = "surveillance" | "prêt" | "expiré" | "invalidé";

export interface AnalysisSetup {
  id: string;
  created_at: string;
  expires_at: string | null;
  symbol: string;
  side: "BUY" | "SELL";
  style: TradingMode;
  strategy: string;
  regime: string;
  entry_price: number;
  stop_loss: number;
  take_profit: number | null;
  volume: number;
  risk_amount: number;
  risk_pct: number;
  net_reward_risk: number | null;
  status: SetupStatus;
  reason: string;
  notes: string[];
  pros: string[];
  cons: string[];
  /** Déjà envoyé depuis le dashboard (bouton « Exécuter ce setup en Live ») */
  executed: boolean;
  /** TP1 / TP2 des sorties partielles (null si désactivées) et TP3 = TP de l'ordre */
  take_profits?: { tp1: number | null; tp2: number | null; tp3: number | null };
}

/** GET /api/analysis/events : fiche structurée d'une annonce économique */
export interface EventCard {
  kind: string;
  fact: string;
  published_at: string | null;
  received_at: string | null;
  source: string;
  reliability: "élevée" | "moyenne" | "faible";
  assets: string[];
  surprise: { actual: number; consensus: number; difference: number; relative_pct: number | null; direction: string } | null;
  effects: Record<string, { direction: "haussier" | "baissier" | "neutre"; strength: number }>;
  horizon: string;
  setup_impacts: { setup_id: string; symbol: string; side: "BUY" | "SELL"; verdict: string; reason: string }[];
  notes: string[];
  intensity: "faible" | "moyenne" | "forte";
  confidence: number;
  valid_until: string | null;
  action: "autoriser" | "attendre" | "bloquer" | "réévaluer";
  action_reason: string;
}

export interface EventCards {
  generated_at: string;
  calendar_available: boolean;
  events: EventCard[];
}

export type Trend = "haussier" | "baissier" | "range";

export interface StructureZone {
  kind: string;
  direction: "haussier" | "baissier";
  low: number;
  high: number;
  time: string;
  active: boolean;
  /** Zone d'offre / de demande posée sur un niveau de l'unité de temps supérieure */
  major?: boolean;
}

/** GET /api/analysis/structure */
export interface MarketStructureReport {
  symbol: string;
  timeframe: string;
  summary: string;
  trend: Trend;
  last_break: { kind: "BOS" | "CHoCH"; direction: "haussier" | "baissier"; level: number; time: string } | null;
  breaks: { kind: "BOS" | "CHoCH"; direction: "haussier" | "baissier"; level: number; time: string }[];
  fair_value_gaps: StructureZone[];
  order_blocks: StructureZone[];
  levels: { kind: "support" | "résistance"; price: number; touches: number; liquidity: boolean }[];
  bollinger: { upper?: number; middle?: number; lower?: number; regime?: string; width_percentile?: number; position?: string };
  atr: number | null;
  notes: string[];
  context: {
    dominant: Trend | "mixte";
    aligned: boolean;
    timeframes: Record<string, { direction: Trend; reason: string }>;
  };
  /** Direction hebdomadaire, règle des 3 bougies (absente sur une ancienne version) */
  weekly?: { direction: Trend; reason: string; doji: number | null };
}

export interface SetupsReport {
  generated_at: string;
  broker: string;
  min_net_reward_risk: number;
  setups: AnalysisSetup[];
  errors: string[];
  /** Exécution manuelle possible (mode LIVE, compte démo) ; sinon le motif */
  execution: { allowed: boolean; reason: string | null };
}

/** POST /api/analysis/setups/{id}/execute */
export interface ExecuteSetupResult {
  success: boolean;
  message: string;
  setup_id: string;
  symbol: string;
  side: "BUY" | "SELL";
  ticket: number;
  volume: number;
  price: number;
  sl: number;
  tp: number;
  checks: string[];
}

export interface SwingPivotOut {
  kind: "high" | "low";
  time: number;
  price: number;
  confirmed_at: number;
}

/** GET /api/analysis/indicators : séries alignées sur `times` (null pendant le préchauffage) */
export interface Indicators {
  symbol: string;
  timeframe: string;
  times: number[];
  atr: (number | null)[];
  adx: (number | null)[];
  plus_di: (number | null)[];
  minus_di: (number | null)[];
  pivots: SwingPivotOut[];
  /** Pivots de la séance précédente (S2, S1, P, R1, R2) */
  levels: { name: string; price: number }[];
  range_bounds: [number, number] | null;
  regime: string;
  structure: string;
  reasons: string[];
}

export interface StrategyStats {
  trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
  net_profit: number;
  expectancy: number;
  expectancy_r: number;
  avg_win: number;
  avg_loss: number;
  profit_factor: number | null;
  max_drawdown: number;
  max_losing_streak: number;
  ambiguous: number;
}

export interface BacktestReport {
  file: string;
  kind: string;
  symbol: string;
  generated_at: string;
  start: string | null;
  end: string | null;
  initial_balance: number;
  stats: StrategyStats;
  equity_drawdown: number;
  equity_drawdown_pct: number;
  /** stratégie, actif, horizon, régime, sens */
  segments: Record<string, Record<string, StrategyStats>>;
  decisions: Record<string, number>;
  equity: { time: number; equity: number }[];
}

export interface PerformanceReport {
  live: { overall: StrategyStats; by_strategy: Record<string, StrategyStats> };
  backtests: BacktestReport[];
}
