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
  suspended: { label: "Suspendu", className: "bg-rose-500/15 text-rose-300" },
};

const STAGES: [string, string][] = [
  ["actualités", "Actualités"],
  ["analyse", "Analyse"],
  ["risque", "Risque"],
  ["ordre", "Ordre"],
  ["tour", "Tour"],
];

/** Décision prise pour chaque symbole lors du dernier tour du moteur. */
export function CycleOutcomes({ status }: { status: Status | null }) {
  const outcomes = status?.last_outcomes ?? [];
  // Modes d'opportunité (impulsion, petits trades) : tous partagent la même liste de symboles autorisés
  const modes = status?.opportunities ?? [];
  const allowed = modes[0]?.symbols ?? [];
  const excluded = (status?.symbols ?? []).filter((s) => !allowed.includes(s));
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
      {status?.suspension && (
        <p className="border-t border-line bg-rose-500/10 px-4 py-2.5 text-xs text-rose-200 sm:px-5">{status.suspension}</p>
      )}
      {status?.latency && Object.keys(status.latency).length > 0 && (
        <div className="border-t border-line px-4 py-2.5 sm:px-5">
          <p className="text-[11px] tracking-[0.06em] text-slate-500 uppercase">Latence par étape (moyenne · p95)</p>
          <dl className="mt-1 grid grid-cols-2 gap-x-4 gap-y-0.5 font-mono text-xs text-slate-400 tabular-nums sm:grid-cols-5">
            {STAGES.filter(([key]) => status.latency?.[key]).map(([key, label]) => (
              <div key={key} className="flex justify-between gap-2 sm:block">
                <dt className="text-slate-500">{label}</dt>
                <dd className="text-slate-200">
                  {status.latency![key].avg_ms} · {status.latency![key].p95_ms} ms
                </dd>
              </div>
            ))}
          </dl>
          {status.rules_version && <p className="mt-1 font-mono text-[11px] text-slate-600">{status.rules_version}</p>}
        </div>
      )}
      {modes.length > 0 && (
        <div className="border-t border-line px-4 py-2.5 sm:px-5">
          <p className="text-[11px] tracking-[0.06em] text-slate-500 uppercase">
            Trades d'opportunité (si Hold) · {allowed.join(", ") || "aucun symbole"}
            {excluded.length > 0 && ` · exclus : ${excluded.join(", ")}`}
          </p>
          <ul className="mt-1 space-y-0.5">
            {modes.map((m) => (
              <li key={m.name} className="text-xs text-slate-400">
                <span className="font-semibold text-slate-200">{m.label}</span> : {m.role}
              </li>
            ))}
          </ul>
        </div>
      )}
    </GlassCard>
  );
}
