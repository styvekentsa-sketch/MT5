import { Landmark, PiggyBank, Scale, TrendingDown, TrendingUp } from "lucide-react";
import type { ReactNode } from "react";
import type { Account, AccountType } from "../api/types";
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
      <div className="flex items-center justify-between gap-2 text-xs font-medium tracking-[0.08em] text-slate-400 uppercase">
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

const ACCOUNT_TYPES: Record<AccountType, { label: string; className: string } | null> = {
  real: { label: "Réel", className: "border-rose-500/50 bg-rose-500/10 text-rose-300" },
  demo: { label: "Démo", className: "border-sky-500/40 bg-sky-500/10 text-sky-300" },
  contest: { label: "Concours", className: "border-violet-500/40 bg-violet-500/10 text-violet-300" },
  paper: { label: "Paper", className: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300" },
  unknown: null,
};

function Tag({ children, className = "border-line-strong text-slate-300" }: { children: ReactNode; className?: string }) {
  return (
    <span className={`inline-flex items-center rounded border px-2 py-0.5 font-mono text-[10px] font-medium tracking-[0.08em] uppercase ${className}`}>
      {children}
    </span>
  );
}

/**
 * Identité du compte connecté : titulaire (ou « Compte démo #login »), broker / exchange,
 * serveur, numéro de compte, type (réel / démo / paper) et état de la connexion.
 */
function AccountIdentity({ account, connected }: { account: Account; connected: boolean | null }) {
  const isMt5 = !account.broker || account.broker === "mt5";
  const title = account.display_name || account.name || (account.login ? `Compte #${account.login}` : "Compte");
  const details = [account.company, account.server, account.login ? `#${account.login}` : null].filter(Boolean);
  const type = ACCOUNT_TYPES[account.account_type ?? "unknown"];
  const link =
    connected === null
      ? { dot: "bg-slate-500", text: "text-slate-400", label: "État inconnu" }
      : connected
        ? { dot: "bg-emerald-400", text: "text-emerald-300", label: "Connecté" }
        : { dot: "bg-rose-500", text: "text-rose-300", label: "Déconnecté" };

  return (
    <div className="flex flex-col gap-3 rounded-md border border-line bg-surface px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:px-5">
      <div className="min-w-0">
        <div className="flex items-center gap-2.5">
          <span
            className={`inline-flex shrink-0 items-center gap-1.5 text-xs font-medium tracking-[0.08em] uppercase ${link.text}`}
            title={isMt5 ? "Connexion du terminal MetaTrader 5 au serveur du broker" : "Connexion à l'exchange"}
          >
            <span className="relative flex size-2">
              {connected && <span className={`absolute inline-flex size-full animate-ping rounded-full opacity-60 ${link.dot}`} />}
              <span className={`relative inline-flex size-2 rounded-full ${link.dot}`} />
            </span>
            {link.label}
          </span>
          <span className="h-3 w-px bg-line-strong" aria-hidden />
          <span className="text-xs font-medium tracking-[0.08em] text-slate-500 uppercase">
            {isMt5 ? "MetaTrader 5" : `${account.broker} · ccxt`}
          </span>
        </div>
        <h2 className="mt-1.5 truncate text-lg font-semibold text-white sm:text-xl" title={title}>
          {title}
        </h2>
        {details.length > 0 && (
          <p className="mt-0.5 font-mono text-xs break-words text-slate-400 sm:truncate" title={details.join(" · ")}>
            {details.join(" · ")}
          </p>
        )}
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-1.5">
        {type && <Tag className={type.className}>{type.label}</Tag>}
        {account.market && <Tag>{account.market === "crypto" ? "Crypto" : "Forex / CFD"}</Tag>}
        <Tag>{account.currency} · 1:{account.leverage}</Tag>
      </div>
    </div>
  );
}

interface AccountOverviewProps {
  account: Account | null;
  /** Connexion au broker (MT5 ou exchange) ; null si inconnue */
  connected?: boolean | null;
}

/** Identité du compte et cartes financières : solde, fonds propres, marge libre, P/L du jour. */
export function AccountOverview({ account, connected = null }: AccountOverviewProps) {
  if (!account) {
    return (
      <div className="flex flex-col gap-4 lg:gap-6">
        <div className="rounded-md border border-line bg-surface px-4 py-4 sm:px-5">
          <Skeleton className="h-3 w-28" />
          <Skeleton className="mt-3 h-5 w-48" />
          <Skeleton className="mt-2 h-3 w-64" />
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:gap-6 xl:grid-cols-4">
          {Array.from({ length: 4 }, (_, i) => (
            <div key={i} className="rounded-md border border-line bg-surface p-4 sm:p-5">
              <Skeleton className="h-3 w-24" />
              <Skeleton className="mt-4 h-7 w-36" />
              <Skeleton className="mt-2 h-3 w-20" />
            </div>
          ))}
        </div>
      </div>
    );
  }

  const money = (v: number) => formatMoney(v, account.currency);
  const equityDelta = account.equity - account.balance;
  const dailyUp = account.daily_pl >= 0;

  return (
    <div className="flex flex-col gap-4 lg:gap-6">
      <AccountIdentity account={account} connected={connected} />
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:gap-6 xl:grid-cols-4">
        <MetricCard
          label="Solde"
          icon={<Landmark className="size-4" />}
          value={money(account.balance)}
          sub={`${account.company || `Compte #${account.login}`} · levier 1:${account.leverage}`}
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
    </div>
  );
}
