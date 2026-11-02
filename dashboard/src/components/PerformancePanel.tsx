import { BarChart3 } from "lucide-react";
import { useMemo, useState } from "react";
import type { BacktestReport, PerformanceReport, StrategyStats } from "../api/types";
import { formatClock, formatDayIfNotToday, formatNumber, formatSigned, formatTime } from "../lib/format";
import { GlassCard, Skeleton } from "./ui/GlassCard";

interface PerformancePanelProps {
  report: PerformanceReport | null;
  currency: string;
  lastUpdated: Date | null;
}

type Row = { label: string; stats: StrategyStats };

const SEGMENTS = ["stratégie", "actif", "horizon", "régime", "sens"] as const;

/**
 * Barres horizontales d'une mesure, une barre par groupe (même axe pour tous).
 * `signed` : axe centré sur zéro, gains et pertes de part et d'autre (espérance).
 */
function MetricBars({ title, rows, value, format, signed = false, tone }: {
  title: string;
  rows: Row[];
  value: (s: StrategyStats) => number;
  format: (v: number) => string;
  signed?: boolean;
  tone: (v: number) => string;
}) {
  const extent = Math.max(...rows.map((r) => Math.abs(value(r.stats))), 1e-9);
  return (
    <figure className="min-w-0">
      <figcaption className="mb-2 text-[11px] tracking-[0.06em] text-slate-500 uppercase">{title}</figcaption>
      <ul className="space-y-1.5">
        {rows.map((r) => {
          const v = value(r.stats);
          const width = `${(Math.abs(v) / extent) * (signed ? 50 : 100)}%`;
          return (
            <li key={r.label} className="group grid grid-cols-[minmax(0,7rem)_1fr_4.5rem] items-center gap-2" title={`${r.label} : ${format(v)} (${r.stats.trades} trades)`}>
              <span className="truncate text-xs text-slate-300">{r.label}</span>
              <span className="relative h-3">
                {signed && <span className="absolute inset-y-0 left-1/2 w-px bg-line-strong" aria-hidden />}
                <span
                  className={`absolute inset-y-0 transition-opacity group-hover:opacity-80 ${tone(v)} ${
                    signed ? (v >= 0 ? "left-1/2 rounded-r" : "right-1/2 rounded-l") : "left-0 rounded-r"
                  }`}
                  style={{ width }}
                />
              </span>
              <span className="text-right font-mono text-xs text-slate-100 tabular-nums">{format(v)}</span>
            </li>
          );
        })}
      </ul>
    </figure>
  );
}

/** Courbe d'équité d'un backtest (SVG), avec repère et valeur au survol. */
function EquityCurve({ points, currency }: { points: BacktestReport["equity"]; currency: string }) {
  const [hover, setHover] = useState<number | null>(null);
  if (points.length < 2) return null;
  const width = 600;
  const height = 140;
  const values = points.map((p) => p.equity);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const x = (i: number) => (i / (points.length - 1)) * width;
  const y = (v: number) => height - 6 - ((v - min) / span) * (height - 12);
  const path = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join("");
  const active = hover === null ? null : points[hover];
  return (
    <figure className="min-w-0">
      <figcaption className="mb-2 flex items-baseline justify-between gap-2 text-[11px] tracking-[0.06em] text-slate-500 uppercase">
        <span>Équité du backtest</span>
        <span className="font-mono tracking-normal text-slate-300 normal-case tabular-nums">
          {active
            ? `${formatDayIfNotToday(new Date(active.time * 1000).toISOString())} ${formatClock(new Date(active.time * 1000).toISOString())} · ${formatNumber(active.equity, 2)} ${currency}`
            : `${formatNumber(min, 2)} – ${formatNumber(max, 2)} ${currency}`}
        </span>
      </figcaption>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        className="h-36 w-full"
        role="img"
        aria-label="Courbe d'équité"
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const box = e.currentTarget.getBoundingClientRect();
          const ratio = (e.clientX - box.left) / box.width;
          setHover(Math.max(0, Math.min(points.length - 1, Math.round(ratio * (points.length - 1)))));
        }}
      >
        <line x1={0} x2={width} y1={y(points[0].equity)} y2={y(points[0].equity)} stroke="var(--color-line-strong)" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />
        <path d={path} fill="none" stroke="var(--color-sky-300, #9fb6d1)" strokeWidth={2} vectorEffect="non-scaling-stroke" />
        {hover !== null && (
          <line x1={x(hover)} x2={x(hover)} y1={0} y2={height} stroke="var(--color-slate-500)" vectorEffect="non-scaling-stroke" />
        )}
      </svg>
    </figure>
  );
}

function StatsTable({ rows, currency }: { rows: Row[]; currency: string }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[36rem] text-left font-mono text-xs tabular-nums">
        <thead className="text-[10px] tracking-[0.06em] text-slate-500 uppercase">
          <tr className="border-b border-line">
            <th className="py-1.5 pr-3 font-medium">Groupe</th>
            <th className="py-1.5 pr-3 text-right font-medium">Trades</th>
            <th className="py-1.5 pr-3 text-right font-medium">Réussite</th>
            <th className="py-1.5 pr-3 text-right font-medium">Espérance</th>
            <th className="py-1.5 pr-3 text-right font-medium">R</th>
            <th className="py-1.5 pr-3 text-right font-medium">Gain / perte moy.</th>
            <th className="py-1.5 pr-3 text-right font-medium">PF</th>
            <th className="py-1.5 pr-3 text-right font-medium">Drawdown</th>
            <th className="py-1.5 text-right font-medium">Série</th>
          </tr>
        </thead>
        <tbody className="text-slate-200">
          {rows.map(({ label, stats: s }) => (
            <tr key={label} className="border-b border-line/60">
              <td className="py-1.5 pr-3 font-sans">{label}</td>
              <td className="py-1.5 pr-3 text-right">{s.trades}</td>
              <td className="py-1.5 pr-3 text-right">{formatNumber(s.win_rate_pct, 1)} %</td>
              <td className="py-1.5 pr-3 text-right">{formatSigned(s.expectancy)} {currency}</td>
              <td className="py-1.5 pr-3 text-right">{formatSigned(s.expectancy_r)}</td>
              <td className="py-1.5 pr-3 text-right">
                {formatNumber(s.avg_win, 2)} / {formatNumber(s.avg_loss, 2)}
              </td>
              <td className="py-1.5 pr-3 text-right">{s.profit_factor === null ? "—" : formatNumber(s.profit_factor, 2)}</td>
              <td className="py-1.5 pr-3 text-right">{formatNumber(s.max_drawdown, 2)}</td>
              <td className="py-1.5 text-right">{s.max_losing_streak}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Performances par stratégie : trades réels clos (journal d'apprentissage) ou rapports de
 * backtest (`python -m backtesting.setup_engine`), segmentables par actif, horizon, régime, sens.
 */
export function PerformancePanel({ report, currency, lastUpdated }: PerformancePanelProps) {
  const [source, setSource] = useState<string>("live");
  const [segment, setSegment] = useState<(typeof SEGMENTS)[number]>("stratégie");
  const backtest = report?.backtests.find((b) => b.file === source) ?? null;

  const rows: Row[] = useMemo(() => {
    if (!report) return [];
    const groups = backtest ? (backtest.segments[segment] ?? {}) : report.live.by_strategy;
    return Object.entries(groups).map(([label, stats]) => ({ label, stats }));
  }, [report, backtest, segment]);
  const overall = backtest ? backtest.stats : report?.live.overall;

  const select = "h-9 rounded-md border border-line bg-surface-2 px-2 text-xs text-slate-200";
  return (
    <GlassCard
      title="Performances & backtests"
      icon={<BarChart3 className="size-4" />}
      actions={<span className="hidden text-xs text-slate-400 sm:inline">{formatTime(lastUpdated)}</span>}
    >
      {report === null ? (
        <div className="space-y-3 p-4 sm:p-5">
          <Skeleton className="h-24" />
        </div>
      ) : (
        <div className="space-y-5 p-4 sm:p-5">
          <div className="flex flex-wrap items-center gap-2">
            <label className="sr-only" htmlFor="perf-source">Source</label>
            <select id="perf-source" className={select} value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="live">Trades réels clos</option>
              {report.backtests.map((b) => (
                <option key={b.file} value={b.file}>
                  Backtest {b.symbol} · {b.start?.slice(0, 10)} → {b.end?.slice(0, 10)}
                </option>
              ))}
            </select>
            {backtest && (
              <div className="inline-flex overflow-hidden rounded-md border border-line" role="group" aria-label="Segmentation">
                {SEGMENTS.map((s) => (
                  <button
                    key={s}
                    type="button"
                    onClick={() => setSegment(s)}
                    aria-pressed={s === segment}
                    className={`h-9 px-2.5 text-xs ${s === segment ? "bg-line text-white" : "text-slate-400 hover:bg-surface-2"}`}
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>

          {overall && (
            <p className="font-mono text-xs text-slate-400 tabular-nums">
              {overall.trades} trades · réussite {formatNumber(overall.win_rate_pct, 1)} % · espérance{" "}
              {formatSigned(overall.expectancy)} {currency} ({formatSigned(overall.expectancy_r)} R) · drawdown max{" "}
              {formatNumber(backtest ? backtest.equity_drawdown : overall.max_drawdown, 2)}
              {backtest && ` (${formatNumber(backtest.equity_drawdown_pct, 2)} %)`}
              {backtest && overall.ambiguous > 0 && ` · ${overall.ambiguous} SL/TP ambigus`}
            </p>
          )}

          {rows.length === 0 ? (
            <p className="text-sm text-slate-400">
              {backtest
                ? "Aucun trade dans ce backtest : les règles d'entrée n'ont trouvé aucun setup exécutable sur la période."
                : "Aucun trade clos dans le journal. Lancez un backtest : python -m backtesting.setup_engine --symbol EURUSD --mt5"}
            </p>
          ) : (
            <>
              <div className="grid gap-5 lg:grid-cols-3">
                <MetricBars
                  title="Espérance nette (R)"
                  rows={rows}
                  value={(s) => s.expectancy_r}
                  format={(v) => formatSigned(v)}
                  signed
                  tone={(v) => (v >= 0 ? "bg-emerald-400" : "bg-rose-400")}
                />
                <MetricBars
                  title="Taux de réussite"
                  rows={rows}
                  value={(s) => s.win_rate_pct}
                  format={(v) => `${formatNumber(v, 1)} %`}
                  tone={() => "bg-slate-400"}
                />
                <MetricBars
                  title={`Drawdown max (${currency})`}
                  rows={rows}
                  value={(s) => s.max_drawdown}
                  format={(v) => formatNumber(v, 0)}
                  tone={() => "bg-rose-300"}
                />
              </div>
              <StatsTable rows={rows} currency={currency} />
            </>
          )}

          {backtest && <EquityCurve points={backtest.equity} currency={currency} />}
          {backtest && (
            <p className="text-[11px] text-slate-500">
              Décisions : {Object.entries(backtest.decisions).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"}
            </p>
          )}
        </div>
      )}
    </GlassCard>
  );
}
