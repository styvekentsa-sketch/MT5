import { Bot, CandlestickChart, User, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { Position } from "../api/types";
import { formatMoney, formatNumber, formatPct, formatSignedMoney, signColor, timeAgo } from "../lib/format";
import { Button } from "./ui/Button";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { GlassCard, Skeleton } from "./ui/GlassCard";

interface PositionsTableProps {
  positions: Position[] | null;
  currency: string;
  /** Solde du compte : base du P/L en % (impact de chaque trade sur le capital) */
  balance: number | null;
  disabled: boolean;
  onClose: (ticket: number) => Promise<void>;
}

function TypeBadge({ type }: { type: Position["type"] }) {
  return (
    <span
      className={`rounded-md px-2 py-0.5 text-xs font-bold ${
        type === "BUY" ? "bg-emerald-500/15 text-emerald-300" : "bg-rose-500/15 text-rose-300"
      }`}
    >
      {type === "BUY" ? "ACHAT" : "VENTE"}
    </span>
  );
}

/** P/L flottant (swap inclus) : montant au centime, % du capital, éclair vert / rouge à chaque variation. */
function LivePnl({ value, currency, balance, className = "" }: { value: number; currency: string; balance: number | null; className?: string }) {
  const previous = useRef(value);
  const [flash, setFlash] = useState<{ dir: "up" | "down"; n: number } | null>(null);
  useEffect(() => {
    if (value !== previous.current) {
      const dir = value > previous.current ? "up" : "down";
      previous.current = value;
      setFlash((f) => ({ dir, n: (f?.n ?? 0) + 1 }));
    }
  }, [value]);
  const pct = balance && balance > 0 ? (value / balance) * 100 : null;
  return (
    <span
      key={flash?.n ?? 0}
      className={`inline-flex flex-col rounded px-1.5 py-0.5 font-mono tabular-nums ${flash ? (flash.dir === "up" ? "flash-up" : "flash-down") : ""} ${className}`}
    >
      <span className={`font-semibold ${signColor(value)}`}>{formatSignedMoney(value, currency)}</span>
      {pct !== null && <span className={`text-[11px] ${signColor(value)}`}>{formatPct(pct, 3)}</span>}
    </span>
  );
}

function OriginIcon({ isBot }: { isBot: boolean }) {
  return isBot ? (
    <Bot className="size-3.5 text-sky-400" aria-label="Ouverte par le bot" />
  ) : (
    <User className="size-3.5 text-slate-500" aria-label="Ouverte manuellement" />
  );
}

const price = (v: number | null) => (v === null ? "—" : formatNumber(v));

/** Positions ouvertes : tableau sur grand écran, cartes sur mobile ; clôture unitaire confirmée. */
export function PositionsTable({ positions, currency, balance, disabled, onClose }: PositionsTableProps) {
  const [pending, setPending] = useState<Position | null>(null);
  const [closing, setClosing] = useState(false);

  const totalProfit = positions?.reduce((sum, p) => sum + p.profit + p.swap, 0) ?? 0;
  const money = (v: number) => formatMoney(v, currency);

  const confirmClose = async () => {
    if (!pending) return;
    setClosing(true);
    try {
      await onClose(pending.ticket);
      setPending(null);
    } finally {
      setClosing(false);
    }
  };

  return (
    <GlassCard
      title={`Positions ouvertes${positions ? ` (${positions.length})` : ""}`}
      icon={<CandlestickChart className="size-4" />}
      actions={
        positions && positions.length > 0 ? (
          <LivePnl value={totalProfit} currency={currency} balance={balance} className="items-end text-sm" />
        ) : null
      }
    >
      {positions === null ? (
        <div className="space-y-3 p-4 sm:p-5">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
        </div>
      ) : positions.length === 0 ? (
        <p className="px-4 sm:px-5 py-10 text-center text-sm text-slate-400">Aucune position ouverte.</p>
      ) : (
        <>
          {/* Grand écran : tableau */}
          <div className="hidden overflow-x-auto overscroll-x-contain xl:block">
            <table className="w-full min-w-[44rem] text-sm [&_tr>*:first-child]:pl-4 sm:[&_tr>*:first-child]:pl-5 [&_tr>*:last-child]:pr-4 sm:[&_tr>*:last-child]:pr-5">
              <thead>
                <tr className="text-left text-xs tracking-wide text-slate-400 uppercase">
                  {["Position", "Volume", "Entrée / Actuel", "SL / TP", "P/L flottant", ""].map((h) => (
                    <th key={h} className="px-4 py-3 font-medium whitespace-nowrap">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-line font-mono tabular-nums">
                {positions.map((p) => (
                  <tr key={p.ticket} className="transition hover:bg-surface-2">
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2 font-sans">
                        <span className="font-semibold text-white">{p.symbol}</span>
                        <TypeBadge type={p.type} />
                      </div>
                      <div className="mt-0.5 flex items-center gap-1.5 text-xs text-slate-500">
                        <OriginIcon isBot={p.is_bot} />#{p.ticket}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-slate-200">{formatNumber(p.volume, 2)}</td>
                    <td className="px-4 py-3 whitespace-nowrap">
                      <div className="text-slate-200">{price(p.price_open)}</div>
                      <div className="text-xs text-slate-400">{price(p.price_current)}</div>
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap">
                      <div className="text-rose-300/80">{price(p.sl)}</div>
                      <div className="text-xs text-emerald-300/80">{price(p.tp)}</div>
                    </td>
                    <td className="px-4 py-2 whitespace-nowrap">
                      <LivePnl value={p.profit + p.swap} currency={currency} balance={balance} />
                    </td>
                    <td className="px-4 py-3 text-right">
                      <Button size="sm" disabled={disabled} onClick={() => setPending(p)} icon={<X className="size-3.5" />}>
                        Fermer
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Mobile : cartes */}
          <ul className="divide-y divide-line xl:hidden">
            {positions.map((p) => (
              <li key={p.ticket} className="space-y-2 px-4 py-3 sm:px-5">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <OriginIcon isBot={p.is_bot} />
                    <span className="font-semibold text-white">{p.symbol}</span>
                    <TypeBadge type={p.type} />
                    <span className="font-mono text-xs text-slate-400">{formatNumber(p.volume, 2)} lot</span>
                  </div>
                  <LivePnl value={p.profit + p.swap} currency={currency} balance={balance} className="items-end" />
                </div>
                <div className="grid grid-cols-2 gap-x-4 gap-y-1 font-mono text-xs text-slate-400 tabular-nums">
                  <span>Entrée {price(p.price_open)}</span>
                  <span>Actuel {price(p.price_current)}</span>
                  <span className="text-rose-300/80">SL {price(p.sl)}</span>
                  <span className="text-emerald-300/80">TP {price(p.tp)}</span>
                </div>
                <div className="flex items-center justify-between text-xs text-slate-500">
                  <span>
                    #{p.ticket} · {timeAgo(p.opened_at)}
                  </span>
                  <Button size="md" disabled={disabled} onClick={() => setPending(p)} icon={<X className="size-4" />}>
                    Fermer
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      <ConfirmDialog
        open={pending !== null}
        title="Fermer la position"
        tone="warning"
        confirmLabel="Fermer la position"
        busy={closing}
        description={
          pending && (
            <p>
              Fermer <strong className="text-white">#{pending.ticket}</strong> — {pending.type}{" "}
              {formatNumber(pending.volume, 2)} {pending.symbol} au prix du marché (P/L actuel{" "}
              <span className={signColor(pending.profit)}>{money(pending.profit)}</span>). La clôture est réelle,
              même en mode PAPER.
            </p>
          )
        }
        onCancel={() => setPending(null)}
        onConfirm={confirmClose}
      />
    </GlassCard>
  );
}
