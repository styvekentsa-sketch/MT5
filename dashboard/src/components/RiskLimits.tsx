import { ShieldAlert, ShieldCheck } from "lucide-react";
import type { RiskLimits as RiskLimitsData } from "../api/types";
import { formatNumber } from "../lib/format";
import { GlassCard, Skeleton } from "./ui/GlassCard";

interface RiskLimitsProps {
  limits: RiskLimitsData | null;
}

/** Jauge d'une limite : vert, puis ambre aux 2/3, puis rouge (et libellé) une fois atteinte. */
function Gauge({ label, value, max, display, reached, reachedText = "Limite atteinte : aucun nouvel ordre aujourd'hui" }: {
  label: string;
  value: number;
  max: number;
  display: string;
  reached: boolean;
  reachedText?: string;
}) {
  const ratio = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
  const tone = reached ? "bg-rose-400" : ratio >= 2 / 3 ? "bg-amber-400" : "bg-emerald-400";
  return (
    <div className="min-w-0">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[11px] tracking-[0.06em] text-slate-500 uppercase">{label}</span>
        <span className="font-mono text-sm text-slate-100 tabular-nums">{display}</span>
      </div>
      <div
        className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-line"
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={value}
      >
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${ratio * 100}%` }} />
      </div>
      {reached && <p className="mt-1 text-xs text-rose-300">{reachedText}</p>}
    </div>
  );
}

/** Limites de risque : perte du jour, pertes consécutives, drawdown et exposition corrélée par devise. */
export function RiskLimits({ limits }: RiskLimitsProps) {
  const blocked =
    limits !== null && (limits.daily_loss_reached || limits.consecutive_losses_reached || !!limits.drawdown_reached);
  const topExposure = limits?.currency_exposure?.[0];
  return (
    <GlassCard
      title="Limites de risque"
      icon={blocked ? <ShieldAlert className="size-4 text-rose-300" /> : <ShieldCheck className="size-4" />}
      actions={
        limits && (
          <span className={`font-mono text-[11px] uppercase ${blocked ? "text-rose-300" : "text-emerald-300"}`}>
            {blocked ? "Bloqué" : "Actif"}
          </span>
        )
      }
    >
      {limits === null ? (
        <div className="grid gap-4 p-4 sm:grid-cols-2 sm:p-5">
          <Skeleton className="h-10" />
          <Skeleton className="h-10" />
        </div>
      ) : (
        <div className="grid gap-4 p-4 sm:grid-cols-2 sm:p-5">
          <Gauge
            label="Perte du jour"
            value={Math.max(0, limits.daily_loss_pct ?? 0)}
            max={limits.max_daily_loss_pct}
            display={`${formatNumber(Math.max(0, limits.daily_loss_pct ?? 0), 2)} % / ${limits.max_daily_loss_pct} %`}
            reached={limits.daily_loss_reached}
          />
          <Gauge
            label="Pertes consécutives"
            value={limits.consecutive_losses}
            max={limits.max_consecutive_losses}
            display={`${limits.consecutive_losses} / ${limits.max_consecutive_losses}`}
            reached={limits.consecutive_losses_reached}
          />
          {limits.max_drawdown_pct !== undefined && limits.max_drawdown_pct > 0 && (
            <Gauge
              label="Drawdown"
              value={Math.max(0, limits.drawdown_pct ?? 0)}
              max={limits.max_drawdown_pct}
              display={`${formatNumber(Math.max(0, limits.drawdown_pct ?? 0), 2)} % / ${limits.max_drawdown_pct} %`}
              reached={!!limits.drawdown_reached}
              reachedText="Drawdown maximal atteint : nouvelles entrées suspendues"
            />
          )}
          {limits.max_currency_risk_pct !== undefined && (
            <Gauge
              label={`Exposition corrélée${topExposure ? ` · ${topExposure.exposure}` : ""}`}
              value={topExposure?.risk_pct ?? 0}
              max={limits.max_currency_risk_pct}
              display={`${formatNumber(topExposure?.risk_pct ?? 0, 2)} % / ${limits.max_currency_risk_pct} %`}
              reached={(topExposure?.risk_pct ?? 0) >= limits.max_currency_risk_pct}
              reachedText="Plus de nouvelle position dans le même sens sur cette devise"
            />
          )}
          <p className="font-mono text-[11px] text-slate-500 tabular-nums sm:col-span-2">
            {limits.risk_per_trade_pct} % par position · {limits.max_total_risk_pct} % cumulé ·{" "}
            {limits.max_open_positions} positions max · {limits.deals_today} transaction(s) aujourd'hui
          </p>
        </div>
      )}
    </GlassCard>
  );
}
