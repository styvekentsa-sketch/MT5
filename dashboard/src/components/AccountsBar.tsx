import { Users } from "lucide-react";
import type { AccountSummary } from "../api/types";
import { formatMoney, formatPct, signColor } from "../lib/format";

interface AccountsBarProps {
  accounts: AccountSummary[];
  selected: string;
  onSelect: (id: string) => void;
}

function stateDot(account: AccountSummary): { className: string; label: string } {
  if (!account.reachable) return { className: "bg-rose-500", label: "Injoignable" };
  if (!account.mt5_connected) return { className: "bg-amber-400", label: "MT5 déconnecté" };
  if (account.state === "paused") return { className: "bg-amber-400", label: "En pause" };
  if (account.state === "running") return { className: "bg-emerald-400", label: "En marche" };
  return { className: "bg-slate-500", label: "Arrêté" };
}

/**
 * Comptes MetaTrader gérés par le bot (mode multi-comptes) : état, mode, solde, P/L du jour,
 * positions. Le compte sélectionné pilote tout le reste du dashboard.
 */
export function AccountsBar({ accounts, selected, onSelect }: AccountsBarProps) {
  return (
    <section aria-label="Comptes" className="flex flex-col gap-2">
      <h2 className="flex items-center gap-2 text-xs font-semibold tracking-[0.08em] text-slate-400 uppercase">
        <Users className="size-4 text-slate-500" aria-hidden />
        Comptes ({accounts.length})
      </h2>
      <div className="-mx-4 flex snap-x gap-3 overflow-x-auto px-4 pb-1 md:mx-0 md:grid md:grid-cols-[repeat(auto-fill,minmax(16rem,1fr))] md:overflow-visible md:px-0" role="tablist">
        {accounts.map((account) => {
          const active = account.id === selected;
          const dot = stateDot(account);
          const money = (v: number | null) => (v === null ? "—" : formatMoney(v, account.currency ?? "USD"));
          return (
            <button
              key={account.id}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => onSelect(account.id)}
              className={`w-64 shrink-0 snap-start rounded-md border bg-surface p-3.5 text-left transition md:w-auto ${
                active ? "border-sky-500/70 ring-1 ring-sky-500/40" : "border-line hover:border-line-strong"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-sm font-semibold text-white">{account.label}</span>
                {account.mode && (
                  <span className={`shrink-0 rounded px-1.5 py-0.5 font-mono text-[10px] font-medium ${
                    account.mode === "LIVE" ? "bg-rose-500/20 text-rose-300" : "bg-emerald-500/15 text-emerald-300"
                  }`}>
                    {account.mode}
                  </span>
                )}
              </div>
              <p className="mt-0.5 truncate font-mono text-[11px] text-slate-500">
                {account.login} · {account.server}
              </p>
              <div className="mt-2.5 flex items-baseline justify-between gap-2 font-mono tabular-nums">
                <span className="text-base text-white">{money(account.balance)}</span>
                {account.daily_pl !== null && (
                  <span className={`text-xs ${signColor(account.daily_pl)}`}>
                    {account.daily_pl > 0 ? "+" : ""}
                    {money(account.daily_pl)}
                    {account.daily_pl_pct !== null && ` (${formatPct(account.daily_pl_pct)})`}
                  </span>
                )}
              </div>
              <div className="mt-2 flex items-center justify-between text-[11px] text-slate-400">
                <span className="inline-flex items-center gap-1.5">
                  <span className={`size-1.5 rounded-full ${dot.className}`} aria-hidden />
                  {dot.label}
                </span>
                <span>{account.open_positions ?? "—"} position(s)</span>
              </div>
              {account.error && <p className="mt-1.5 truncate text-[11px] text-rose-300" title={account.error}>{account.error}</p>}
            </button>
          );
        })}
      </div>
    </section>
  );
}
