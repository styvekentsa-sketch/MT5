import { CalendarDays, Rss, Webhook } from "lucide-react";
import type { FeedKind, FeedSource, FeedStatus } from "../../api/types";
import { timeAgo } from "../../lib/format";
import { Skeleton } from "../ui/GlassCard";

const STATUS: Record<FeedStatus, { label: string; dot: string; text: string }> = {
  ok: { label: "OK", dot: "bg-emerald-400", text: "text-emerald-300" },
  error: { label: "Erreur", dot: "bg-rose-500", text: "text-rose-300" },
  pending: { label: "En attente", dot: "bg-slate-400", text: "text-slate-300" },
  disabled: { label: "Désactivé", dot: "bg-slate-600", text: "text-slate-500" },
};

const KIND_ICON: Record<FeedKind, typeof Rss> = { rss: Rss, calendar: CalendarDays, webhook: Webhook };

/** État de chaque source lue par le bot : dernière lecture, prochaine lecture, volume, erreur. */
export function SourcesStrip({ sources }: { sources: FeedSource[] | null }) {
  if (!sources) {
    return (
      <div className="flex gap-3 overflow-hidden">
        {Array.from({ length: 3 }, (_, i) => (
          <Skeleton key={i} className="h-24 w-64 shrink-0 rounded-md" />
        ))}
      </div>
    );
  }

  return (
    <ul
      className="-mx-4 flex snap-x scroll-px-4 gap-4 overflow-x-auto overscroll-x-contain px-4 pb-1 lg:mx-0 lg:grid lg:scroll-px-0 lg:grid-cols-[repeat(auto-fill,minmax(15rem,1fr))] lg:gap-6 lg:overflow-visible lg:px-0"
      aria-label="Sources de données"
    >
      {sources.map((source) => {
        const Icon = KIND_ICON[source.kind];
        const status = STATUS[source.status];
        return (
          <li
            key={`${source.kind}-${source.url ?? source.name}`}
            className="w-64 shrink-0 snap-start rounded-md border border-line bg-surface p-4 lg:w-auto"
            title={source.url ?? undefined}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="flex min-w-0 items-center gap-2 text-sm font-semibold text-slate-200">
                <Icon className="size-4 shrink-0 text-sky-400" aria-hidden />
                <span className="truncate">{source.name}</span>
              </span>
              <span className={`inline-flex shrink-0 items-center gap-1.5 text-xs font-semibold ${status.text}`}>
                <span className={`size-2 rounded-full ${status.dot}`} aria-hidden />
                {status.label}
              </span>
            </div>
            <dl className="mt-3 grid grid-cols-3 gap-2 text-[11px] text-slate-400">
              <div>
                <dt className="text-slate-500">Lu</dt>
                <dd className="text-slate-300">{timeAgo(source.last_fetch)}</dd>
              </div>
              <div>
                <dt className="text-slate-500">Prochaine</dt>
                <dd className="text-slate-300">{source.next_fetch ? timeAgo(source.next_fetch) : "—"}</dd>
              </div>
              <div>
                <dt className="text-slate-500">Éléments</dt>
                <dd className="font-mono text-slate-300 tabular-nums">{source.items}</dd>
              </div>
            </dl>
            {source.detail && (
              <p
                className={`mt-2 truncate text-[11px] ${source.status === "error" ? "text-rose-300" : "text-slate-500"}`}
                title={source.detail}
              >
                {source.detail}
              </p>
            )}
          </li>
        );
      })}
    </ul>
  );
}
