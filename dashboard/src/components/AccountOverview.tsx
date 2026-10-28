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
  accent?: string;
}

function MetricCard({ label, icon, value, sub, accent = "from-sky-500/20" }: MetricCardProps) {
  return (
    <div
      className={`relative overflow-hidden rounded-2xl border border-white/10 bg-gradient-to-br ${accent} to-white/[0.02] p-5 backdrop-blur-xl`}
    >
      <div className="flex items-center justify-between text-xs font-medium tracking-wide text-slate-400 uppercase">
        {label}
        <span className="text-slate-500">{icon}</span>
      </div>
      <div className="mt-3 font-mono text-2xl font-semibold text-white tabular-nums">{value}</div>
      {sub && <div className="mt-1 text-xs text-slate-400 tabular-nums">{sub}</div>}
    </div>
  );
}

/** Cartes financières du compte : solde, fonds propres, marge libre, P/L du jour. */
export function AccountOverview({ account }: { account: Account | null }) {
  if (!account) {
    return (
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="rounded-2xl border border-white/10 bg-white/[0.04] p-5">
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
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <MetricCard
        label="Solde"
        icon={<Landmark className="size-4" />}
        value={money(account.balance)}
        sub={`Compte #${account.login} · levier 1:${account.leverage}`}
      />
      <MetricCard
        label="Fonds propres"
        icon={<Scale className="size-4" />}
        accent="from-indigo-500/20"
        value={money(account.equity)}
        sub={<span className={signColor(equityDelta)}>{equityDelta >= 0 ? "+" : ""}{money(equityDelta)} latent</span>}
      />
      <MetricCard
        label="Marge libre"
        icon={<PiggyBank className="size-4" />}
        accent="from-violet-500/20"
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
        accent={dailyUp ? "from-emerald-500/20" : "from-rose-500/25"}
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
