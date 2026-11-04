import { CheckCircle2, Crosshair, RefreshCw, Rocket, ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";
import type { AnalysisSetup, SetupsReport } from "../api/types";
import { formatClock, formatNumber, formatTime } from "../lib/format";
import { Button } from "./ui/Button";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { GlassCard, Skeleton } from "./ui/GlassCard";

interface SetupsPanelProps {
  report: SetupsReport | null;
  currency: string;
  lastUpdated: Date | null;
  onRefresh: () => void;
  /** Envoie l'ordre du setup ; la promesse se résout une fois la réponse du serveur reçue */
  onExecute?: (setup: AnalysisSetup) => Promise<void>;
}

/** Motif empêchant d'exécuter ce setup depuis le dashboard, ou null. */
function executionBlock(s: AnalysisSetup, report: SetupsReport): string | null {
  if (s.executed) return "Déjà exécuté";
  if (s.status !== "prêt") return "Seul un setup prêt peut être exécuté";
  if (s.expires_at && new Date(s.expires_at).getTime() < Date.now()) return "Setup expiré : relancez l'analyse";
  if (s.take_profit === null || !(s.stop_loss > 0) || !(s.entry_price > 0)) return "Niveaux de prix invalides";
  if (!report.execution.allowed) return report.execution.reason ?? "Exécution indisponible";
  return null;
}

const STATUS_STYLE: Record<AnalysisSetup["status"], string> = {
  prêt: "border-emerald-500/50 bg-emerald-500/15 text-emerald-300",
  surveillance: "border-sky-500/40 bg-sky-500/10 text-sky-200",
  invalidé: "border-line text-slate-400",
  expiré: "border-line text-slate-500",
};

function Level({ label, value, className = "text-slate-100" }: { label: string; value: string; className?: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] tracking-[0.06em] text-slate-500 uppercase">{label}</div>
      <div className={`truncate font-mono text-sm tabular-nums ${className}`}>{value}</div>
    </div>
  );
}

function SetupCard({ s, currency, minRatio, block, pending, onExecute }: {
  s: AnalysisSetup;
  currency: string;
  minRatio: number;
  block: string | null;
  pending: boolean;
  onExecute?: () => void;
}) {
  const price = (v: number | null) => (v === null ? "—" : formatNumber(v, 5));
  const ratioOk = s.net_reward_risk !== null && s.net_reward_risk >= minRatio;
  return (
    <li className={`px-4 py-3 sm:px-5 ${s.status === "prêt" ? "bg-emerald-500/5" : ""}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-slate-100">{s.symbol}</span>
        <span className={`font-mono text-[11px] uppercase ${s.side === "BUY" ? "text-emerald-300" : "text-rose-300"}`}>
          {s.side === "BUY" ? "Achat" : "Vente"}
        </span>
        <span className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase ${STATUS_STYLE[s.status]}`}>
          {s.status}
        </span>
        <span className="font-mono text-[11px] text-slate-400">
          {s.strategy} · {s.style} · {s.regime}
        </span>
        {s.expires_at && s.status !== "expiré" && (
          <span className="ml-auto font-mono text-[11px] text-slate-500">valable jusqu'à {formatClock(s.expires_at)}</span>
        )}
      </div>

      <div className="mt-2 grid grid-cols-3 gap-3 sm:grid-cols-6">
        <Level label="Entrée" value={price(s.entry_price)} />
        <Level label="Stop loss" value={price(s.stop_loss)} className="text-rose-300" />
        <Level
          label={s.take_profits?.tp1 != null ? "TP1 · TP2 · TP3" : "Take profit"}
          value={
            s.take_profits?.tp1 != null
              ? [s.take_profits.tp1, s.take_profits.tp2, s.take_profit].filter((v) => v != null).map((v) => price(v)).join(" · ")
              : price(s.take_profit)
          }
          className="text-emerald-300"
        />
        <Level
          label="Ratio net"
          value={s.net_reward_risk === null ? "—" : formatNumber(s.net_reward_risk, 2)}
          className={ratioOk ? "text-slate-100" : "text-amber-300"}
        />
        <Level label="Lot" value={s.volume > 0 ? formatNumber(s.volume, 2) : "—"} />
        <Level
          label="Risque"
          value={s.risk_amount > 0 ? `${formatNumber(s.risk_amount, 2)} ${currency} · ${formatNumber(s.risk_pct, 2)} %` : "—"}
        />
      </div>

      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <ul className="space-y-0.5" aria-label="Arguments favorables">
          {s.pros.map((text, i) => (
            <li key={i} className="flex gap-1.5 text-xs text-slate-300">
              <ThumbsUp className="mt-0.5 size-3 shrink-0 text-emerald-400" aria-hidden />
              <span className="min-w-0 break-words">{text}</span>
            </li>
          ))}
        </ul>
        <ul className="space-y-0.5" aria-label="Arguments défavorables">
          {s.cons.length === 0 && <li className="text-xs text-slate-500">Aucun argument défavorable relevé</li>}
          {s.cons.map((text, i) => (
            <li key={i} className="flex gap-1.5 text-xs text-slate-300">
              <ThumbsDown className="mt-0.5 size-3 shrink-0 text-rose-400" aria-hidden />
              <span className="min-w-0 break-words">{text}</span>
            </li>
          ))}
        </ul>
      </div>

      {s.status === "prêt" && onExecute && (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          {s.executed ? (
            <span className="inline-flex items-center gap-1.5 text-xs text-emerald-300">
              <CheckCircle2 className="size-4" aria-hidden /> Ordre envoyé depuis le dashboard
            </span>
          ) : (
            <>
              <Button
                variant="success"
                size="sm"
                icon={<Rocket className="size-4" aria-hidden />}
                loading={pending}
                disabled={block !== null}
                title={block ?? "Envoyer cet ordre au marché sur MT5 (revérifié au prix actuel)"}
                onClick={onExecute}
              >
                {pending ? "Envoi en cours…" : "Exécuter ce setup en Live"}
              </Button>
              {block && <span className="text-xs text-slate-500">{block}</span>}
            </>
          )}
        </div>
      )}
    </li>
  );
}

/**
 * Mode analyste : propositions des stratégies à setups (pullback, rejet de range), calculées
 * par le serveur avec la même séquence qu'en direct (risque, vérificateur, fenêtre d'annonces).
 * Aucun ordre n'est passé depuis ce panneau.
 */
export function SetupsPanel({ report, currency, lastUpdated, onRefresh, onExecute }: SetupsPanelProps) {
  const ready = report?.setups.filter((s) => s.status === "prêt").length ?? 0;
  const [confirming, setConfirming] = useState<AnalysisSetup | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);

  // Requête asynchrone : l'interface reste utilisable pendant l'envoi (bouton en état « Envoi en cours »)
  const execute = async (setup: AnalysisSetup) => {
    if (!onExecute) return;
    setConfirming(null);
    setPendingId(setup.id);
    try {
      await onExecute(setup);
    } finally {
      setPendingId(null);
    }
  };
  const price = (v: number | null) => (v === null ? "—" : formatNumber(v, 5));
  return (
    <GlassCard
      title="Setups & analyses"
      icon={<Crosshair className="size-4" />}
      actions={
        <span className="flex items-center gap-3 text-xs text-slate-400">
          {report && <span className="font-mono tabular-nums">{ready} prêt(s)</span>}
          <span className="hidden sm:inline">{formatTime(lastUpdated)}</span>
          <button
            type="button"
            onClick={onRefresh}
            className="grid size-9 place-items-center rounded-md text-slate-400 hover:bg-line hover:text-slate-100"
            aria-label="Relancer l'analyse"
          >
            <RefreshCw className="size-4" />
          </button>
        </span>
      }
    >
      {report === null ? (
        <div className="space-y-3 p-4 sm:p-5">
          <Skeleton className="h-20" />
          <Skeleton className="h-20" />
        </div>
      ) : report.setups.length === 0 ? (
        <p className="px-4 py-6 text-sm text-slate-400 sm:px-5">
          Aucune proposition pour l'instant : les stratégies attendent un contexte valide (tendance avec retest, ou range
          confirmé) et un ratio net d'au moins {report.min_net_reward_risk}.
        </p>
      ) : (
        <ul className="divide-y divide-line">
          {report.setups.map((s) => (
            <SetupCard
              key={s.id}
              s={s}
              currency={currency}
              minRatio={report.min_net_reward_risk}
              block={executionBlock(s, report)}
              pending={pendingId === s.id}
              onExecute={onExecute ? () => setConfirming(s) : undefined}
            />
          ))}
        </ul>
      )}
      {report && report.errors.length > 0 && (
        <p className="border-t border-line px-4 py-2 text-xs text-amber-200 sm:px-5">
          Analyse incomplète : {report.errors.join(" · ")}
        </p>
      )}

      <ConfirmDialog
        open={confirming !== null}
        tone="warning"
        title="Exécuter ce setup en Live"
        confirmLabel="Envoyer l'ordre"
        description={
          confirming && (
            <>
              <p>
                Un ordre <strong className="text-white">réel</strong> au marché sera envoyé sur MetaTrader 5. Le serveur
                revérifie tout au prix actuel (mode, compte, expiration, écart de prix, risque, vérificateur) et refuse
                l'ordre si une règle n'est plus respectée.
              </p>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 rounded-md border border-line bg-canvas p-3 font-mono text-xs tabular-nums">
                <dt className="text-slate-500">Actif</dt>
                <dd className="text-white">
                  {confirming.symbol} · {confirming.side === "BUY" ? "Achat" : "Vente"}
                </dd>
                <dt className="text-slate-500">Prix d'entrée prévu</dt>
                <dd className="text-white">{price(confirming.entry_price)}</dd>
                <dt className="text-slate-500">Stop loss</dt>
                <dd className="text-rose-300">{price(confirming.stop_loss)}</dd>
                <dt className="text-slate-500">Take profit</dt>
                <dd className="text-emerald-300">{price(confirming.take_profit)}</dd>
                <dt className="text-slate-500">Volume indicatif</dt>
                <dd className="text-white">{formatNumber(confirming.volume, 2)} lot (recalculé à l'envoi)</dd>
                <dt className="text-slate-500">Risque</dt>
                <dd className="text-white">
                  {formatNumber(confirming.risk_amount, 2)} {currency} · {formatNumber(confirming.risk_pct, 2)} %
                </dd>
              </dl>
            </>
          )
        }
        onConfirm={() => confirming && void execute(confirming)}
        onCancel={() => setConfirming(null)}
      />
    </GlassCard>
  );
}
