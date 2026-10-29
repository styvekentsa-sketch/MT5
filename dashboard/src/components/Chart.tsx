import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  LineStyle,
  type CreatePriceLineOptions,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { CandlestickChart, LoaderCircle } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, errorMessage, type Connection } from "../api/client";
import type { Candle, Position } from "../api/types";
import { formatNumber, formatPrice, signColor } from "../lib/format";
import { GlassCard } from "./ui/GlassCard";

const TIMEFRAMES = ["M1", "M5", "M15", "H1"] as const;
type Timeframe = (typeof TIMEFRAMES)[number];

const HISTORY_COUNT = 300;           // chargement initial
const LIVE_COUNT = 3;                // rafraîchissement : bougie en cours + 2 précédentes (clôtures tardives)
const LIVE_POLL_MS = 2_000;
const RETRY_MS = 5_000;

// Charte « terminal institutionnel » (cf. index.css)
const THEME = {
  background: "#111317",
  text: "#8a8c90",
  grid: "#1a1d22",
  border: "#23272e",
  crosshair: "#4d5158",
  label: "#24272d",
  up: "#4ea37a",
  down: "#c76a62",
  // Niveaux des positions : teintes claires, distinctes des bougies
  stopLoss: "#d4928c",
  takeProfit: "#86bfa1",
  entry: "#7895b8",
} as const;

type LevelKind = "entry" | "sl" | "tp";

const LEVEL_STYLE: Record<LevelKind, { color: string; lineStyle: LineStyle }> = {
  entry: { color: THEME.entry, lineStyle: LineStyle.Solid },
  sl: { color: THEME.stopLoss, lineStyle: LineStyle.Dashed },
  tp: { color: THEME.takeProfit, lineStyle: LineStyle.Dashed },
};

/**
 * Lignes à tracer pour les positions du symbole affiché : entrée, SL et TP (si définis).
 * Clé stable « ticket:type » pour mettre à jour ou supprimer une ligne sans la recréer.
 * Rendu : libellé court sur la ligne (« SL ») collé au prix coloré sur l'échelle (« 1.08500 »).
 * lightweight-charts n'affiche le titre qu'avec l'étiquette d'axe : celle-ci porte donc le prix.
 */
function positionLevels(positions: Position[]): Map<string, CreatePriceLineOptions> {
  const levels = new Map<string, CreatePriceLineOptions>();
  const tagged = positions.length > 1;                       // plusieurs positions : on précise le ticket
  for (const p of positions) {
    const tag = tagged ? ` · #${p.ticket}` : "";
    const add = (kind: LevelKind, price: number | null, title: string) => {
      if (price === null || !Number.isFinite(price) || price <= 0) return;
      levels.set(`${p.ticket}:${kind}`, {
        price,
        ...LEVEL_STYLE[kind],
        lineWidth: 1,
        lineVisible: true,
        axisLabelVisible: true,
        axisLabelColor: LEVEL_STYLE[kind].color,
        axisLabelTextColor: THEME.background,
        title: `${title}${tag}`,
      });
    };
    add("entry", p.price_open, `${p.type} ${p.volume}`);
    add("sl", p.sl, "SL");
    add("tp", p.tp, "TP");
  }
  return levels;
}

type FeedState =
  | { kind: "loading" }
  | { kind: "live" }
  /** stale : des bougies sont déjà affichées (on les garde visibles pendant la reconnexion) */
  | { kind: "error"; title: string; hint: string; stale: boolean };

/** Cause lisible d'un échec, pour savoir quoi faire sans ouvrir les logs. */
function describeError(error: unknown): { title: string; hint: string } {
  if (error instanceof ApiError) {
    if (error.status === 404 && /not found/i.test(error.message)) {
      return {
        title: "Route des cours absente de l'API",
        hint: "Le bot en cours d'exécution est antérieur à cette fonctionnalité : redémarrez-le (Ctrl+C puis .\\scripts\\start.ps1).",
      };
    }
    if (error.status === 404) return { title: error.message, hint: "Vérifiez TRADING_SYMBOLS dans le .env du bot." };
    if (error.kind === "unavailable") {
      return { title: "MetaTrader 5 indisponible", hint: `${error.message}. Le terminal MT5 doit être ouvert et connecté.` };
    }
    if (error.kind === "unauthorized") return { title: "Clé API refusée", hint: "Vérifiez la clé dans les paramètres de connexion." };
    if (error.kind === "network") return { title: "API injoignable", hint: `${error.message}. Le bot est-il lancé ?` };
  }
  return { title: "Cours indisponibles", hint: errorMessage(error) };
}

interface Quote {
  bid: number | null;
  last: number;
  changePct: number;           // variation depuis l'ouverture de la première bougie affichée
  digits: number;
}

const toBar = (c: Candle) => ({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close });

interface ChartProps {
  connection: Connection;
  symbols: string[];
  /** Positions ouvertes (toutes paires) : SL, TP et entrée du symbole affiché sont tracés */
  positions: Position[] | null;
  /** False quand le graphique n'est pas à l'écran : aucune requête */
  active: boolean;
}

/** Graphique en bougies des symboles suivis, mis à jour en direct depuis l'API du bot. */
export function PriceChart({ connection, symbols, positions, active }: ChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const [symbol, setSymbol] = useState<string>("");
  const [timeframe, setTimeframe] = useState<Timeframe>("M15");
  const [feed, setFeed] = useState<FeedState>({ kind: "loading" });
  const [quote, setQuote] = useState<Quote | null>(null);
  const linesRef = useRef(new Map<string, { line: IPriceLine; signature: string }>());
  const levelPricesRef = useRef<number[]>([]);       // pris en compte par l'échelle verticale

  const symbolPositions = useMemo(
    () => (positions ?? []).filter((p) => p.symbol === symbol),
    [positions, symbol],
  );

  // Symbole par défaut : le premier suivi par le bot
  useEffect(() => {
    if (symbols.length > 0 && !symbols.includes(symbol)) setSymbol(symbols[0]);
  }, [symbols, symbol]);

  // Création unique du graphique
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: THEME.background },
        textColor: THEME.text,
        fontFamily: "IBM Plex Mono, ui-monospace, monospace",
        fontSize: 11,
        attributionLogo: true,       // attribution TradingView requise par la licence
      },
      grid: { vertLines: { color: THEME.grid }, horzLines: { color: THEME.grid } },
      rightPriceScale: { borderColor: THEME.border },
      timeScale: { borderColor: THEME.border, timeVisible: true, secondsVisible: false, rightOffset: 4 },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: THEME.crosshair, labelBackgroundColor: THEME.label },
        horzLine: { color: THEME.crosshair, labelBackgroundColor: THEME.label },
      },
    });
    seriesRef.current = chart.addSeries(CandlestickSeries, {
      upColor: THEME.up,
      downColor: THEME.down,
      borderUpColor: THEME.up,
      borderDownColor: THEME.down,
      wickUpColor: THEME.up,
      wickDownColor: THEME.down,
      // L'échelle s'élargit pour que SL et TP restent visibles même hors de la plage des bougies
      autoscaleInfoProvider: (original: () => { priceRange: { minValue: number; maxValue: number } | null } | null) => {
        const info = original();
        const levels = levelPricesRef.current;
        if (!info?.priceRange || levels.length === 0) return info;
        return {
          ...info,
          priceRange: {
            minValue: Math.min(info.priceRange.minValue, ...levels),
            maxValue: Math.max(info.priceRange.maxValue, ...levels),
          },
        };
      },
    });
    chartRef.current = chart;
    const lines = linesRef.current;
    return () => {
      chart.remove();                // détruit aussi les lignes de prix
      lines.clear();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // Chargement de l'historique puis mise à jour de la dernière bougie, tant que le graphique est visible
  useEffect(() => {
    if (!active || !symbol || !connection.apiKey) return;
    const controller = new AbortController();
    let timer: number | undefined;
    let lastTime = 0;
    let firstOpen = 0;
    let loaded = false;

    const schedule = (delay: number) => {
      if (!controller.signal.aborted) timer = window.setTimeout(tick, delay);
    };

    const publishQuote = (candles: Candle[], bid: number | null, digits: number) => {
      const last = candles[candles.length - 1];
      if (!last || !firstOpen) return;
      setQuote({ bid, last: last.close, changePct: ((last.close - firstOpen) / firstOpen) * 100, digits });
    };

    const load = async () => {
      setFeed({ kind: "loading" });
      setQuote(null);
      const data = await api.candles(connection, symbol, timeframe, HISTORY_COUNT, controller.signal);
      const series = seriesRef.current;
      if (controller.signal.aborted || !series) return;
      series.applyOptions({ priceFormat: { type: "price", precision: data.digits, minMove: 10 ** -data.digits } });
      series.setData(data.candles.map(toBar));
      chartRef.current?.timeScale().scrollToRealTime();
      firstOpen = data.candles[0]?.open ?? 0;
      lastTime = data.candles[data.candles.length - 1]?.time ?? 0;
      loaded = true;
      publishQuote(data.candles, data.bid, data.digits);
    };

    const refresh = async () => {
      const data = await api.candles(connection, symbol, timeframe, LIVE_COUNT, controller.signal);
      const series = seriesRef.current;
      if (controller.signal.aborted || !series) return;
      // update() remplace la bougie de même heure ou en ajoute une nouvelle ; jamais de bougie plus ancienne
      for (const candle of data.candles) {
        if (candle.time >= lastTime) {
          series.update(toBar(candle));
          lastTime = candle.time;
        }
      }
      publishQuote(data.candles, data.bid, data.digits);
    };

    let busy = false;
    async function tick() {
      if (busy || controller.signal.aborted) return;
      if (document.hidden) {
        schedule(LIVE_POLL_MS);
        return;
      }
      busy = true;
      try {
        await (loaded ? refresh() : load());
        if (!controller.signal.aborted) setFeed({ kind: "live" });
        schedule(LIVE_POLL_MS);
      } catch (error) {
        if (controller.signal.aborted) return;
        setFeed({ kind: "error", ...describeError(error), stale: loaded });
        schedule(RETRY_MS);              // reconnexion automatique
      } finally {
        busy = false;
      }
    }

    // Retour au premier plan ou du réseau : nouvelle tentative immédiate, sans attendre RETRY_MS
    const wake = () => {
      if (document.hidden || !navigator.onLine) return;
      window.clearTimeout(timer);
      void tick();
    };
    document.addEventListener("visibilitychange", wake);
    window.addEventListener("online", wake);

    void tick();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", wake);
      window.removeEventListener("online", wake);
    };
  }, [active, symbol, timeframe, connection]);

  // Lignes SL / TP / entrée : créées, mises à jour ou supprimées à chaque rafraîchissement des positions
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    const wanted = positionLevels(symbolPositions);
    const lines = linesRef.current;
    for (const [key, { line }] of lines) {
      if (!wanted.has(key)) {                  // position clôturée, ou SL / TP retiré
        series.removePriceLine(line);
        lines.delete(key);
      }
    }
    for (const [key, options] of wanted) {
      const signature = `${options.price}|${options.title}`;
      const existing = lines.get(key);
      if (!existing) {
        lines.set(key, { line: series.createPriceLine(options), signature });
      } else if (existing.signature !== signature) {    // SL / TP déplacé
        existing.line.applyOptions(options);
        existing.signature = signature;
      }
    }
    levelPricesRef.current = [...wanted.values()].map((o) => o.price);
  }, [symbolPositions]);

  // 44 px par défaut (doigt) ; 36 px seulement si une souris est détectée
  const segment = "h-11 min-w-11 px-2.5 text-xs font-medium transition pointer-fine:h-9";

  return (
    <GlassCard
      title="Graphique"
      icon={<CandlestickChart className="size-4" />}
      actions={
        <span className="flex items-center gap-1.5 text-xs text-slate-400">
          {feed.kind === "loading" && <LoaderCircle className="size-3.5 animate-spin" aria-hidden />}
          {feed.kind === "live" && <span className="size-1.5 rounded-full bg-emerald-400" aria-hidden />}
          {feed.kind === "error" && <span className="size-1.5 rounded-full bg-amber-400" aria-hidden />}
          {feed.kind === "live" ? "Direct" : feed.kind === "loading" ? "Chargement" : "Reconnexion…"}
        </span>
      }
    >
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-2.5 sm:px-5">
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex overflow-hidden rounded-md border border-line" role="group" aria-label="Symbole">
            {symbols.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setSymbol(s)}
                aria-pressed={s === symbol}
                className={`${segment} font-mono ${s === symbol ? "bg-line text-white" : "text-slate-400 hover:bg-surface-2"}`}
              >
                {s}
              </button>
            ))}
          </div>
          <div className="inline-flex overflow-hidden rounded-md border border-line" role="group" aria-label="Unité de temps">
            {TIMEFRAMES.map((tf) => (
              <button
                key={tf}
                type="button"
                onClick={() => setTimeframe(tf)}
                aria-pressed={tf === timeframe}
                className={`${segment} font-mono ${tf === timeframe ? "bg-line text-white" : "text-slate-400 hover:bg-surface-2"}`}
              >
                {tf}
              </button>
            ))}
          </div>
        </div>
        {quote && (
          <div className="flex items-baseline gap-3 font-mono tabular-nums">
            {symbolPositions.length > 0 && (
              <span className="self-center rounded border border-line px-1.5 py-0.5 font-sans text-[11px] text-slate-400">
                {symbolPositions.length} position{symbolPositions.length > 1 ? "s" : ""}
              </span>
            )}
            <span className="text-lg font-medium text-white">{formatPrice(quote.last, quote.digits)}</span>
            <span className={`text-xs ${signColor(quote.changePct)}`}>
              {quote.changePct >= 0 ? "+" : ""}
              {formatNumber(quote.changePct, 2)} %
            </span>
          </div>
        )}
      </div>

      <div className="relative">
        {/* Hauteur liée à la fenêtre (portrait, paysage, grands écrans) ; la largeur suit le conteneur (autoSize) */}
        <div ref={containerRef} className="h-[clamp(15rem,45dvh,30rem)] w-full 2xl:h-[clamp(20rem,50dvh,40rem)]" />
        {feed.kind === "error" && !feed.stale && (
          <div className="absolute inset-0 grid place-items-center bg-surface/90 px-6 text-center" role="status">
            <p className="max-w-md text-sm text-slate-200">
              {feed.title}
              <span className="mt-1 block text-xs text-slate-400">{feed.hint}</span>
              <span className="mt-2 block text-xs text-slate-500">Nouvelle tentative toutes les {RETRY_MS / 1000} s.</span>
            </p>
          </div>
        )}
        {feed.kind === "error" && feed.stale && (
          <div className="absolute inset-x-0 top-0 border-b border-amber-500/30 bg-amber-500/10 px-4 py-1.5 text-xs text-amber-200" role="status">
            {feed.title} : dernières bougies affichées, reconnexion automatique.
          </div>
        )}
        {symbols.length === 0 && (
          <div className="absolute inset-0 grid place-items-center text-sm text-slate-500">En attente du statut du bot…</div>
        )}
      </div>
    </GlassCard>
  );
}
