import { ArrowDownRight, ArrowUpRight, ExternalLink, ScrollText } from "lucide-react";
import { useMemo, useState } from "react";
import type { CalendarEvent, NewsItem, SentimentLabel } from "../../api/types";
import { formatClock, formatDayIfNotToday, formatSigned, isSafeUrl, signColor, timeAgo } from "../../lib/format";
import { GlassCard, Skeleton } from "../ui/GlassCard";

type EntryKind = "sentiment" | "economic";
type KindFilter = "all" | EntryKind;

type Entry =
  | { kind: "sentiment"; time: string | null; key: string; symbols: string[]; item: NewsItem }
  | { kind: "economic"; time: string; key: string; symbols: string[]; item: CalendarEvent };

const LABEL_STYLE: Record<SentimentLabel, string> = {
  bullish: "bg-emerald-500/15 text-emerald-300",
  bearish: "bg-rose-500/15 text-rose-300",
  neutral: "bg-slate-500/20 text-slate-300",
};

const KIND_BADGE: Record<EntryKind, { label: string; className: string }> = {
  sentiment: { label: "Sentiment", className: "bg-violet-500/15 text-violet-300" },
  economic: { label: "Économique", className: "bg-amber-500/15 text-amber-300" },
};

function FilterChip({ active, onClick, children }: { active: boolean; onClick: () => void; children: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`h-10 shrink-0 rounded-xl px-3 text-xs font-semibold transition ${
        active ? "bg-sky-500/90 text-white" : "border border-white/10 bg-white/5 text-slate-300 hover:bg-white/10"
      }`}
    >
      {children}
    </button>
  );
}

function SymbolImpact({ symbol, direction }: { symbol: string; direction: number }) {
  const Icon = direction >= 0 ? ArrowUpRight : ArrowDownRight;
  return (
    <span
      className={`inline-flex items-center gap-0.5 rounded-md bg-white/5 px-1.5 py-0.5 text-[11px] font-semibold ${signColor(direction)}`}
      title={direction >= 0 ? "Actif de base concerné (même sens que le sentiment)" : "Devise de cotation concernée (sens inversé)"}
    >
      {symbol}
      <Icon className="size-3" aria-hidden />
    </span>
  );
}

function TimeColumn({ time }: { time: string | null }) {
  return (
    <div className="w-14 shrink-0 text-right">
      <div className="font-mono text-xs font-semibold text-slate-300 tabular-nums">{formatClock(time)}</div>
      <div className="text-[10px] text-slate-500">{formatDayIfNotToday(time)}</div>
    </div>
  );
}

function NewsEntry({ item }: { item: NewsItem }) {
  return (
    <>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className={`rounded-md px-1.5 py-0.5 text-[11px] font-semibold ${KIND_BADGE.sentiment.className}`}>Sentiment</span>
        <span className="rounded-md bg-sky-500/15 px-1.5 py-0.5 text-[11px] font-semibold text-sky-300">{item.source}</span>
        <span className={`rounded-md px-1.5 py-0.5 font-mono text-[11px] font-semibold ${LABEL_STYLE[item.label]}`}>
          {formatSigned(item.score)}
        </span>
      </div>
      {isSafeUrl(item.link) ? (
        <a href={item.link} target="_blank" rel="noopener noreferrer" className="group mt-1 block text-sm font-medium text-slate-100 hover:text-sky-300">
          {item.title}
          <ExternalLink className="ml-1 inline size-3 opacity-0 transition group-hover:opacity-100" aria-hidden />
        </a>
      ) : (
        <p className="mt-1 text-sm font-medium text-slate-100">{item.title}</p>
      )}
      {item.snippet && <p className="mt-0.5 line-clamp-2 text-xs text-slate-400">{item.snippet}</p>}
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        {Object.entries(item.symbols).map(([symbol, direction]) => (
          <SymbolImpact key={symbol} symbol={symbol} direction={direction} />
        ))}
        {item.keywords.map((keyword) => (
          <span key={keyword} className="rounded-md border border-white/10 px-1.5 py-0.5 text-[11px] text-slate-400">
            #{keyword}
          </span>
        ))}
      </div>
      <p className="mt-1 text-[11px] text-slate-500">
        Publié {timeAgo(item.published)} · extrait {timeAgo(item.fetched_at)}
      </p>
    </>
  );
}

function EconomicEntry({ item }: { item: CalendarEvent }) {
  return (
    <>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className={`rounded-md px-1.5 py-0.5 text-[11px] font-semibold ${KIND_BADGE.economic.className}`}>Économique</span>
        <span className="rounded-md bg-sky-500/15 px-1.5 py-0.5 text-[11px] font-semibold text-sky-300">Forex Factory</span>
        <span className="rounded-md bg-white/10 px-1.5 py-0.5 font-mono text-[11px] font-bold text-slate-100">{item.currency}</span>
        <span className="rounded-md bg-rose-500/15 px-1.5 py-0.5 text-[11px] font-semibold text-rose-300">Impact {item.impact}</span>
        {item.blocks_trading && <span className="rounded-md bg-rose-600 px-1.5 py-0.5 text-[11px] font-bold text-white">BLOQUE</span>}
      </div>
      <p className="mt-1 text-sm font-medium text-slate-100">{item.title}</p>
      <p className="mt-0.5 text-xs text-slate-400">
        {timeAgo(item.time)}
        {item.forecast && ` · prévision ${item.forecast}`}
        {item.previous && ` · précédent ${item.previous}`}
      </p>
      {item.symbols.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {item.symbols.map((symbol) => (
            <span key={symbol} className="rounded-md bg-white/5 px-1.5 py-0.5 text-[11px] font-semibold text-amber-200">
              {symbol}
            </span>
          ))}
        </div>
      )}
    </>
  );
}

interface FeedLogProps {
  news: NewsItem[] | null;
  calendar: CalendarEvent[] | null;
  symbols: string[];
}

/** Journal chronologique de tout ce que le bot a capturé (articles et annonces), filtrable. */
export function FeedLog({ news, calendar, symbols }: FeedLogProps) {
  const [symbolFilter, setSymbolFilter] = useState<string>("all");
  const [kindFilter, setKindFilter] = useState<KindFilter>("all");

  const entries = useMemo<Entry[]>(() => {
    const all: Entry[] = [
      ...(news ?? []).map((item, i): Entry => ({
        kind: "sentiment", time: item.published ?? item.fetched_at, key: `n-${i}-${item.link}`,
        symbols: Object.keys(item.symbols), item,
      })),
      ...(calendar ?? []).map((item, i): Entry => ({
        kind: "economic", time: item.time, key: `e-${i}-${item.time}-${item.title}`, symbols: item.symbols, item,
      })),
    ];
    const ts = (time: string | null) => (time ? new Date(time).getTime() : Number.NEGATIVE_INFINITY);
    return all
      .filter((e) => kindFilter === "all" || e.kind === kindFilter)
      .filter((e) => symbolFilter === "all" || e.symbols.includes(symbolFilter))
      .sort((a, b) => ts(b.time) - ts(a.time));
  }, [news, calendar, kindFilter, symbolFilter]);

  return (
    <GlassCard
      title="Flux en direct"
      icon={<ScrollText className="size-4" />}
      actions={<span className="text-xs text-slate-500">{entries.length} entrée(s)</span>}
    >
      <div className="space-y-2 border-b border-white/5 px-4 py-3 sm:px-5">
        <div className="-mx-4 flex gap-2 overflow-x-auto px-4 sm:mx-0 sm:flex-wrap sm:px-0" role="group" aria-label="Filtrer par symbole">
          <FilterChip active={symbolFilter === "all"} onClick={() => setSymbolFilter("all")}>Tous les symboles</FilterChip>
          {symbols.map((symbol) => (
            <FilterChip key={symbol} active={symbolFilter === symbol} onClick={() => setSymbolFilter(symbol)}>
              {symbol}
            </FilterChip>
          ))}
        </div>
        <div className="flex gap-2" role="group" aria-label="Filtrer par type">
          <FilterChip active={kindFilter === "all"} onClick={() => setKindFilter("all")}>Tout</FilterChip>
          <FilterChip active={kindFilter === "sentiment"} onClick={() => setKindFilter("sentiment")}>Sentiment</FilterChip>
          <FilterChip active={kindFilter === "economic"} onClick={() => setKindFilter("economic")}>Économique</FilterChip>
        </div>
      </div>

      {news === null && calendar === null ? (
        <div className="space-y-3 p-5">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      ) : entries.length === 0 ? (
        <p className="px-5 py-10 text-center text-sm text-slate-400">Aucune donnée capturée pour ce filtre.</p>
      ) : (
        <ol className="divide-y divide-white/5 lg:max-h-[52rem] lg:overflow-y-auto lg:overscroll-contain">
          {entries.map((entry) => (
            <li key={entry.key} className="flex gap-3 px-4 py-3 transition hover:bg-white/3 sm:px-5">
              <TimeColumn time={entry.time} />
              <div className="min-w-0 flex-1">
                {entry.kind === "sentiment" ? <NewsEntry item={entry.item} /> : <EconomicEntry item={entry.item} />}
              </div>
            </li>
          ))}
        </ol>
      )}
    </GlassCard>
  );
}
