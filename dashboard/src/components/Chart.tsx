import { CandlestickSeries, ColorType, CrosshairMode, createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { CandlestickChart, LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, errorMessage, type Connection } from "../api/client";
import type { Candle } from "../api/types";
import { formatNumber, signColor } from "../lib/format";
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
} as const;

type FeedState = { kind: "loading" } | { kind: "live" } | { kind: "error"; message: string };

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
  /** False quand le graphique n'est pas à l'écran : aucune requête */
  active: boolean;
}

/** Graphique en bougies des symboles suivis, mis à jour en direct depuis l'API du bot. */
export function PriceChart({ connection, symbols, active }: ChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const [symbol, setSymbol] = useState<string>("");
  const [timeframe, setTimeframe] = useState<Timeframe>("M15");
  const [feed, setFeed] = useState<FeedState>({ kind: "loading" });
  const [quote, setQuote] = useState<Quote | null>(null);

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
    });
    chartRef.current = chart;
    return () => {
      chart.remove();
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

    async function tick() {
      if (document.hidden) {
        schedule(LIVE_POLL_MS);
        return;
      }
      try {
        await (loaded ? refresh() : load());
        if (!controller.signal.aborted) setFeed({ kind: "live" });
        schedule(LIVE_POLL_MS);
      } catch (error) {
        if (controller.signal.aborted) return;
        setFeed({ kind: "error", message: errorMessage(error) });
        schedule(RETRY_MS);
      }
    }

    void tick();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [active, symbol, timeframe, connection]);

  // 36 px à la souris, 44 px sur tout écran tactile (téléphone, tablette, PC tactile)
  const segment = "h-9 min-w-11 px-2.5 text-xs font-medium transition pointer-coarse:h-11";

  return (
    <GlassCard
      title="Graphique"
      icon={<CandlestickChart className="size-4" />}
      actions={
        <span className="flex items-center gap-1.5 text-xs text-slate-400">
          {feed.kind === "loading" && <LoaderCircle className="size-3.5 animate-spin" aria-hidden />}
          {feed.kind === "live" && <span className="size-1.5 rounded-full bg-emerald-400" aria-hidden />}
          {feed.kind === "error" && <span className="size-1.5 rounded-full bg-rose-500" aria-hidden />}
          {feed.kind === "live" ? "Direct" : feed.kind === "loading" ? "Chargement" : "Hors ligne"}
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
            <span className="text-lg font-medium text-white">{formatNumber(quote.last, quote.digits)}</span>
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
        {feed.kind === "error" && (
          <div className="absolute inset-0 grid place-items-center bg-surface/85 px-6 text-center text-sm text-slate-300">
            <p>
              Cours indisponibles : {feed.message}
              <span className="mt-1 block text-xs text-slate-500">
                Nouvelle tentative automatique. Si l'erreur persiste, redémarrez le bot pour activer la route
                /api/market/candles.
              </span>
            </p>
          </div>
        )}
        {symbols.length === 0 && (
          <div className="absolute inset-0 grid place-items-center text-sm text-slate-500">En attente du statut du bot…</div>
        )}
      </div>
    </GlassCard>
  );
}
