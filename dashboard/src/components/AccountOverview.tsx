import { Landmark, PiggyBank, Scale, TrendingDown, TrendingUp } from "lucide-react";
import type { ReactNode } from "react";
import type { Account } from "../api/types";
import { formatMoney, formatNumber, formatPct, signColor } from "../lib/format";
import { Skeleton } from "./ui/GlassCard";

interface MetricCardProps {
  label: string;
  icon: ReactNode;
  value: ReactNode;
  sub?: ReactNode;
  /** Filet vertical coloré (ex : "border-l-emerald-500") pour une métrique signée */
  accent?: string;
}

function MetricCard({ label, icon, value, sub, accent = "border-l-line-strong" }: MetricCardProps) {
  return (
    <div className={`min-w-0 rounded-md border border-l-2 border-line bg-surface px-4 py-3.5 sm:px-5 sm:py-4 ${accent}`}>
      <div className="flex items-center justify-between gap-2 text-[11px] font-medium tracking-[0.08em] text-slate-400 uppercase">
        <span className="truncate">{label}</span>
        <span className="shrink-0 text-slate-600">{icon}</span>
      </div>
      <div className="mt-1.5 overflow-hidden font-mono text-xl font-medium text-ellipsis whitespace-nowrap text-white tabular-nums sm:mt-3 sm:text-2xl xl:text-[1.6rem] xl:leading-8">
        {value}
      </div>
      {sub && <div className="mt-1 text-[11px] text-slate-400 tabular-nums sm:text-xs">{sub}</div>}
    </div>
  );
}

/** Cartes financières du compte : solde, fonds propres, marge libre, P/L du jour. */
export function AccountOverview({ account }: { account: Account | null }) {
  if (!account) {
    return (
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 sm:gap-4 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="rounded-md border border-line bg-surface p-5">
            <Skeleton className="h-3 w-24" />
            <Skeleton className="mt-4 h-7 w-36" />
            <Skeleton className="mt-2 h-3 w-20" />
          </div>
        ))}
      </div>
    );
  }

  const money = (v: number) => formatMoney(v, account.currency);
  const equityDelta = account.equity - account.balance;
  const dailyUp = account.daily_pl >= 0;

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 sm:gap-4 xl:grid-cols-4">
      <MetricCard
        label="Solde"
        icon={<Landmark className="size-4" />}
        value={money(account.balance)}
        sub={`Compte #${account.login} · levier 1:${account.leverage}`}
      />
      <MetricCard
        label="Fonds propres"
        icon={<Scale className="size-4" />}
        value={money(account.equity)}
        sub={<span className={signColor(equityDelta)}>{equityDelta >= 0 ? "+" : ""}{money(equityDelta)} latent</span>}
      />
      <MetricCard
        label="Marge libre"
        icon={<PiggyBank className="size-4" />}
        value={money(account.margin_free)}
        sub={
          account.margin > 0
            ? `Marge utilisée ${money(account.margin)} · niveau ${formatNumber(account.margin_level, 0)} %`
            : "Aucune marge utilisée"
        }
      />
      <MetricCard
        label="P/L du jour"
        icon={dailyUp ? <TrendingUp className="size-4" /> : <TrendingDown className="size-4" />}
        accent={account.daily_pl > 0 ? "border-l-emerald-500" : account.daily_pl < 0 ? "border-l-rose-500" : undefined}
        value={
          <span className={signColor(account.daily_pl)}>
            {account.daily_pl > 0 ? "+" : ""}
            {money(account.daily_pl)}
          </span>
        }
        sub={
          <>
            <span className={signColor(account.daily_pl_pct)}>{formatPct(account.daily_pl_pct)}</span>
            {" · réalisé "}
            {money(account.realized_pl_today)}
          </>
        }
      />
    </div>
  );
}
