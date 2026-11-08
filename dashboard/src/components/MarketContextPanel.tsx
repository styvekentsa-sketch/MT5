import { CalendarClock, Layers } from "lucide-react";
import { useCallback, useState } from "react";
import { api, type Connection } from "../api/client";
import type { EventCard, MarketStructureReport, Trend } from "../api/types";
import { usePolling } from "../hooks/usePolling";
import { formatClock, formatNumber } from "../lib/format";
import { GlassCard, Skeleton } from "./ui/GlassCard";

const STRUCTURE_POLL_MS = 60_000;
const EVENTS_POLL_MS = 60_000;
const TIMEFRAMES = ["M15", "H1", "H4"];

const TREND_STYLE: Record<Trend | "mixte", string> = {
  haussier: "text-emerald-300",
  baissier: "text-rose-300",
  range: "text-slate-300",
  mixte: "text-amber-300",
};

const ACTION_STYLE: Record<EventCard["action"], string> = {
  autoriser: "border-emerald-500/40 text-emerald-300",
  attendre: "border-sky-500/40 text-sky-200",
  bloquer: "border-rose-500/50 text-rose-300",
  réévaluer: "border-amber-500/50 text-amber-300",
};

function Zones({ title, zones }: { title: string; zones: MarketStructureReport["fair_value_gaps"] }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] tracking-[0.06em] text-slate-500 uppercase">{title}</div>
      {zones.length === 0 ? (
        <p className="text-xs text-slate-500">Aucune zone active</p>
      ) : (
        <ul className="mt-0.5 space-y-0.5 font-mono text-xs tabular-nums">
          {zones.slice(0, 3).map((z) => (
            <li key={`${z.kind}-${z.time}`} className={z.direction === "haussier" ? "text-emerald-300" : "text-rose-300"}>
              {formatNumber(z.low, 5)} – {formatNumber(z.high, 5)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function StructureView({ report }: { report: MarketStructureReport }) {
  const ctx = report.context;
  return (
    <div className="space-y-3 px-4 py-3 sm:px-5">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-sm">
        {report.weekly && (
          <span title={`Règle des 3 bougies : ${report.weekly.reason}`}>
            <span className="font-mono text-[11px] text-slate-500">W1</span>{" "}
            <span className={TREND_STYLE[report.weekly.direction]}>{report.weekly.direction}</span>
          </span>
        )}
        {Object.entries(ctx.timeframes).map(([tf, d]) => (
          <span key={tf} title={d.reason}>
            <span className="font-mono text-[11px] text-slate-500">{tf}</span>{" "}
            <span className={TREND_STYLE[d.direction]}>{d.direction}</span>
          </span>
        ))}
        <span className="ml-auto text-xs text-slate-400">
          Contexte <span className={TREND_STYLE[ctx.dominant]}>{ctx.aligned ? `aligné ${ctx.dominant}` : ctx.dominant}</span>
        </span>
      </div>
      <p className="text-xs text-slate-300">
        {report.summary}
        {report.bollinger.position && ` · prix ${report.bollinger.position} des bandes de Bollinger`}
      </p>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <Zones title="Fair Value Gaps" zones={report.fair_value_gaps} />
        <Zones title="Order Blocks" zones={report.order_blocks} />
        <div className="col-span-2 min-w-0 sm:col-span-1">
          <div className="text-[11px] tracking-[0.06em] text-slate-500 uppercase">Niveaux clés</div>
          {report.levels.length === 0 ? (
            <p className="text-xs text-slate-500">Aucun niveau touché deux fois</p>
          ) : (
            <ul className="mt-0.5 space-y-0.5 font-mono text-xs tabular-nums text-slate-300">
              {report.levels.slice(0, 3).map((l) => (
                <li key={`${l.kind}-${l.price}`}>
                  {l.kind} {formatNumber(l.price, 5)} · {l.touches}×{l.liquidity ? " · liquidité" : ""}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      {report.notes.length > 0 && <p className="text-[11px] text-slate-500">{report.notes.join(" · ")}</p>}
    </div>
  );
}

function EventRow({ card }: { card: EventCard }) {
  const effects = Object.entries(card.effects);
  return (
    <li className="px-4 py-2.5 sm:px-5">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase ${ACTION_STYLE[card.action]}`}>
          {card.action}
        </span>
        <span className="min-w-0 text-sm text-slate-100">{card.fact}</span>
      </div>
      <p className="mt-1 font-mono text-[11px] text-slate-400 tabular-nums">
        {card.published_at && `publié ${formatClock(card.published_at)}`}
        {card.received_at && ` · reçu ${formatClock(card.received_at, true)}`} · intensité {card.intensity} · confiance{" "}
        {Math.round(card.confidence * 100)} %{card.valid_until && ` · valable jusqu'à ${formatClock(card.valid_until)}`}
      </p>
      {effects.length > 0 && (
        <p className="mt-0.5 text-xs text-slate-300">
          {effects.map(([symbol, e]) => `${symbol} ${e.direction} (${formatNumber(e.strength, 2)})`).join(" · ")}
        </p>
      )}
      <p className="mt-0.5 text-xs text-slate-500">{card.action_reason}</p>
    </li>
  );
}

/** Contexte de marché : structure multi-unités de temps et fiches des annonces économiques. */
export function MarketContextPanel({ connection, symbols, active }: {
  connection: Connection;
  symbols: string[];
  active: boolean;
}) {
  const [picked, setPicked] = useState<string | null>(null);
  const [timeframe, setTimeframe] = useState("M15");
  const symbol = picked && symbols.includes(picked) ? picked : symbols[0];
  const structure = usePolling(
    useCallback((s: AbortSignal) => api.structure(connection, symbol ?? "", timeframe, s), [connection, symbol, timeframe]),
    STRUCTURE_POLL_MS,
    active && !!symbol,
    `${connection.baseUrl}|${symbol}|${timeframe}`,
  );
  const events = usePolling(
    useCallback((s: AbortSignal) => api.events(connection, s), [connection]),
    EVENTS_POLL_MS,
    active,
    connection.baseUrl,
  );
  const select = "rounded border border-line bg-surface-2 px-2 py-1 font-mono text-xs text-slate-200";

  return (
    <div className="grid gap-4 lg:grid-cols-2 lg:gap-6">
      <GlassCard
        title="Structure de marché"
        icon={<Layers className="size-4" />}
        actions={
          <div className="flex gap-2">
            <select aria-label="Symbole" className={select} value={symbol ?? ""} onChange={(e) => setPicked(e.target.value)}>
              {symbols.map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
            <select aria-label="Unité de temps" className={select} value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
              {TIMEFRAMES.map((tf) => (
                <option key={tf}>{tf}</option>
              ))}
            </select>
          </div>
        }
      >
        {structure.data && structure.data.symbol === symbol ? (
          <StructureView report={structure.data} />
        ) : structure.error ? (
          <p className="px-4 py-6 text-center text-sm text-slate-400 sm:px-5">Structure indisponible : {structure.error.message}</p>
        ) : (
          <div className="space-y-2 p-4 sm:p-5">
            <Skeleton className="h-5" />
            <Skeleton className="h-16" />
          </div>
        )}
      </GlassCard>

      <GlassCard
        title="Annonces économiques"
        icon={<CalendarClock className="size-4" />}
        actions={
          events.data &&
          !events.data.calendar_available && <span className="font-mono text-[11px] text-rose-300 uppercase">Calendrier indisponible</span>
        }
      >
        {events.data === null ? (
          <div className="space-y-2 p-4 sm:p-5">
            <Skeleton className="h-10" />
            <Skeleton className="h-10" />
          </div>
        ) : events.data.events.length === 0 ? (
          <p className="px-4 py-6 text-center text-sm text-slate-400 sm:px-5">
            Aucune annonce importante sur les devises suivies (−6 h / +12 h).
          </p>
        ) : (
          <ul className="divide-y divide-line">
            {events.data.events.map((card) => (
              <EventRow key={`${card.fact}-${card.published_at}`} card={card} />
            ))}
          </ul>
        )}
      </GlassCard>
    </div>
  );
}
