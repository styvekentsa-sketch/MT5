import { ListChecks } from "lucide-react";
import type { Status } from "../api/types";
import { timeAgo } from "../lib/format";
import { GlassCard } from "./ui/GlassCard";

const OUTCOME_STYLES: Record<string, { label: string; className: string }> = {
  traded: { label: "Trade", className: "bg-emerald-500/15 text-emerald-300" },
  hold: { label: "Hold", className: "bg-slate-500/20 text-slate-300" },
  news_blocked: { label: "News", className: "bg-amber-500/15 text-amber-300" },
  cooldown: { label: "Cooldown", className: "bg-sky-500/15 text-sky-300" },
  rejected: { label: "Refusé", className: "bg-orange-500/15 text-orange-300" },
  failed: { label: "Échec", className: "bg-rose-500/15 text-rose-300" },
  error: { label: "Erreur", className: "bg-rose-500/15 text-rose-300" },
};

/** Décision prise pour chaque symbole lors du dernier tour du moteur. */
export function CycleOutcomes({ status }: { status: Status | null }) {
  const outcomes = status?.last_outcomes ?? [];
  return (
    <GlassCard
      title="Dernier tour du moteur"
      icon={<ListChecks className="size-4" />}
      actions={
        status?.last_cycle_at && (
          <span className="text-xs text-slate-500">
            {timeAgo(status.last_cycle_at)}
            {status.last_cycle_duration_seconds !== null && ` · ${status.last_cycle_duration_seconds} s`}
          </span>
        )
      }
    >
      {outcomes.length === 0 ? (
        <p className="px-4 sm:px-5 py-6 text-center text-sm text-slate-400">Aucun tour exécuté pour l'instant.</p>
      ) : (
        <ul className="divide-y divide-line">
          {outcomes.map((o) => {
            const style = OUTCOME_STYLES[o.status] ?? OUTCOME_STYLES.hold;
            return (
              <li key={o.symbol} className="flex items-start gap-3 px-4 sm:px-5 py-2.5 text-sm">
                <span className="w-20 shrink-0 font-semibold text-slate-200">{o.symbol}</span>
                <span className={`shrink-0 rounded-md px-2 py-0.5 text-xs font-semibold ${style.className}`}>
                  {style.label}
                </span>
                <span className="line-clamp-4 text-xs text-slate-400 lg:line-clamp-2" title={o.detail}>
                  {o.detail}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </GlassCard>
  );
}
