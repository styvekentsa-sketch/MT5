import { ScrollText } from "lucide-react";
import type { ReactNode } from "react";
import type { BotDecision, BotDecisions } from "../api/types";
import { formatTime, signColor } from "../lib/format";
import { GlassCard, Skeleton } from "./ui/GlassCard";

export type DecisionFilter = "all" | "accepted" | "rejected";

interface DecisionJournalProps {
  journal: BotDecisions | null;
  filter: DecisionFilter;
  onFilterChange: (filter: DecisionFilter) => void;
  lastUpdated: Date | null;
}

const FILTERS: { id: DecisionFilter; label: string }[] = [
  { id: "all", label: "Tout" },
  { id: "accepted", label: "Acceptés" },
  { id: "rejected", label: "Rejetés" },
];

const STAGE_LABELS: Record<BotDecision["stage"], string> = {
  signal: "Signal",
  calendrier: "Calendrier",
  cooldown: "Cooldown",
  risque: "Risque",
  broker: "Broker",
  exécution: "Exécution",
  erreur: "Erreur",
};

function Badge({ className, children }: { className: string; children: ReactNode }) {
  return (
    <span className={`inline-flex shrink-0 items-center rounded border px-1.5 py-0.5 font-mono text-[10px] font-medium tracking-[0.06em] uppercase ${className}`}>
      {children}
    </span>
  );
}

function ActionBadge({ action }: { action: BotDecision["action"] }) {
  if (!action) return <Badge className="border-line text-slate-500">Aucun</Badge>;
  return action === "ACHAT" ? (
    <Badge className="border-emerald-500/40 text-emerald-300">Achat</Badge>
  ) : (
    <Badge className="border-rose-500/40 text-rose-300">Vente</Badge>
  );
}

function StatusBadge({ status }: { status: BotDecision["status"] }) {
  return status === "ACCEPTÉ" ? (
    <Badge className="border-emerald-500/50 bg-emerald-500/15 text-emerald-300">Accepté</Badge>
  ) : (
    <Badge className="border-rose-500/50 bg-rose-500/15 text-rose-300">Rejeté</Badge>
  );
}

/** Scores de la décision, affichés au survol avec l'explication complète. */
function tooltip(d: BotDecision): string {
  const score = (label: string, v: number | null) => (v === null ? null : `${label} ${v >= 0 ? "+" : ""}${v.toFixed(2)}`);
  const scores = [score("technique", d.technical_score), score("sentiment", d.sentiment_score), score("hybride", d.hybrid_score)]
    .filter(Boolean)
    .join(" · ");
  return [d.reason, scores, d.detail && d.detail !== d.reason ? d.detail : null].filter(Boolean).join("\n\n");
}

function DecisionRow({ d }: { d: BotDecision }) {
  const time = new Date(d.timestamp);
  return (
    <li className="px-4 py-3 transition hover:bg-surface-2 sm:px-5" title={tooltip(d)}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <span className="w-16 shrink-0 text-sm font-semibold text-slate-100">{d.symbol}</span>
        <ActionBadge action={d.action} />
        <StatusBadge status={d.status} />
        <span className="text-[11px] text-slate-500">{STAGE_LABELS[d.stage]}</span>
        <span className="ml-auto shrink-0 font-mono text-[11px] text-slate-500 tabular-nums">
          {Number.isNaN(time.getTime()) ? "—" : time.toLocaleTimeString("fr-FR")}
        </span>
      </div>
      <p className={`mt-1.5 line-clamp-2 text-xs leading-relaxed break-words sm:text-sm ${d.status === "REJETÉ" ? "text-slate-300" : "text-emerald-200"}`}>
        {d.reason}
      </p>
      {d.hybrid_score !== null && (
        <p className="mt-1 font-mono text-[11px] text-slate-500 tabular-nums">
          hybride <span className={signColor(d.hybrid_score)}>{d.hybrid_score >= 0 ? "+" : ""}{d.hybrid_score.toFixed(2)}</span>
          {d.technical_score !== null && ` · technique ${d.technical_score >= 0 ? "+" : ""}${d.technical_score.toFixed(2)}`}
          {d.sentiment_score !== null && ` · sentiment ${d.sentiment_score >= 0 ? "+" : ""}${d.sentiment_score.toFixed(2)}`}
        </p>
      )}
    </li>
  );
}

/**
 * Journal des décisions et rejets : pour chaque symbole évalué à chaque tour du moteur, le sens
 * envisagé, ACCEPTÉ (vert) ou REJETÉ (rouge) et le motif précis. Regroupé par tour, rafraîchi en direct.
 */
export function DecisionJournal({ journal, filter, onFilterChange, lastUpdated }: DecisionJournalProps) {
  // Regroupement par tour ; les numéros repartent de 1 à chaque lancement du bot : un tour plus
  // ancien portant un numéro supérieur ou égal signale un redémarrage
  const groups: { cycle: number | null; items: BotDecision[]; restartAfter: boolean }[] = [];
  for (const d of journal?.items ?? []) {
    const last = groups.at(-1);
    if (last && last.cycle === d.cycle) {
      last.items.push(d);
    } else {
      if (last && last.cycle !== null && d.cycle !== null && d.cycle >= last.cycle) last.restartAfter = true;
      groups.push({ cycle: d.cycle, items: [d], restartAfter: false });
    }
  }

  return (
    <GlassCard
      title="Journal des décisions"
      icon={<ScrollText className="size-4" />}
      actions={
        <span className="flex items-center gap-1.5 text-xs text-slate-400" title="Rafraîchi automatiquement">
          <span className="size-1.5 rounded-full bg-emerald-400" aria-hidden />
          {formatTime(lastUpdated)}
        </span>
      }
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-2.5 sm:px-5">
        <div className="inline-flex overflow-hidden rounded-md border border-line" role="group" aria-label="Filtrer les décisions">
          {FILTERS.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              onClick={() => onFilterChange(id)}
              aria-pressed={filter === id}
              className={`h-11 px-3 text-xs font-medium transition pointer-fine:h-8 ${
                filter === id ? "bg-surface-2 text-white" : "text-slate-400 hover:bg-surface-2 hover:text-slate-200"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        {journal && (
          <span className="font-mono text-[11px] text-slate-500 tabular-nums">
            <span className="text-emerald-300">{journal.accepted}</span> acceptée(s) ·{" "}
            <span className="text-rose-300">{journal.rejected}</span> rejetée(s)
            {journal.source === "memory" && " · mémoire"}
          </span>
        )}
      </div>

      {!journal ? (
        <div className="space-y-3 p-4 sm:p-5">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : journal.items.length === 0 ? (
        <p className="px-4 py-10 text-center text-sm text-slate-400 sm:px-5">
          {filter === "all" ? "Aucune décision enregistrée pour l'instant." : "Aucune décision pour ce filtre."}
        </p>
      ) : (
        <div className="max-h-[70dvh] overflow-y-auto overscroll-contain lg:max-h-[32rem]">
          {groups.map((group, index) => (
            <section key={`${group.cycle}-${index}`} aria-label={group.cycle !== null ? `Tour ${group.cycle}` : "Tour"}>
              <h3 className="sticky top-0 z-10 border-b border-line bg-surface-2/95 px-4 py-1.5 font-mono text-[11px] tracking-[0.06em] text-slate-500 uppercase sm:px-5">
                {group.cycle !== null ? `Tour #${group.cycle}` : "Tour"} ·{" "}
                {new Date(group.items[0].timestamp).toLocaleTimeString("fr-FR")}
              </h3>
              <ul className="divide-y divide-line">
                {group.items.map((d) => (
                  <DecisionRow key={d.id} d={d} />
                ))}
              </ul>
              {group.restartAfter && (
                <p className="border-y border-line bg-canvas px-4 py-1.5 text-center text-[11px] tracking-[0.06em] text-slate-500 uppercase sm:px-5">
                  Redémarrage du bot
                </p>
              )}
            </section>
          ))}
        </div>
      )}
    </GlassCard>
  );
}
