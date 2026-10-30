import { CalendarClock, Ban } from "lucide-react";
import type { CalendarEvent, CalendarStatus } from "../../api/types";
import { formatClock, formatDayIfNotToday, timeAgo } from "../../lib/format";
import { GlassCard, Skeleton } from "../ui/GlassCard";

const STATUS: Record<CalendarStatus, { label: string; dot: string; badge: string }> = {
  imminent: { label: "Imminente", dot: "bg-rose-500 ring-2 ring-rose-500/30", badge: "bg-rose-500/20 text-rose-300" },
  upcoming: { label: "À venir", dot: "bg-amber-400", badge: "bg-amber-500/15 text-amber-300" },
  passed: { label: "Passée", dot: "bg-slate-600", badge: "bg-slate-500/20 text-slate-400" },
};

interface CalendarTimelineProps {
  events: CalendarEvent[] | null;
  blockWindow: [number, number] | null;
}

/** Timeline des annonces à fort impact surveillées, avec celles qui bloquent les nouveaux trades. */
export function CalendarTimeline({ events, blockWindow }: CalendarTimelineProps) {
  const blocking = events?.filter((e) => e.blocks_trading) ?? [];

  return (
    <GlassCard
      title="Calendrier économique"
      icon={<CalendarClock className="size-4" />}
      actions={
        blockWindow && (
          <span className="hidden truncate text-[11px] text-slate-500 sm:inline">
            blocage {blockWindow[0]} min avant → {blockWindow[1]} min après
          </span>
        )
      }
    >
      {events === null ? (
        <div className="space-y-3 p-4 sm:p-5">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
        </div>
      ) : (
        <>
          {blocking.length > 0 && (
            <div className="flex items-start gap-3 border-b border-rose-500/20 bg-rose-500/10 px-4 sm:px-5 py-3 text-sm text-rose-200">
              <Ban className="mt-0.5 size-4 shrink-0" aria-hidden />
              <p>
                <span className="font-semibold">Nouveaux trades bloqués</span> :{" "}
                {blocking.map((e) => `${e.title} (${e.currency}) ${timeAgo(e.time)}`).join(" · ")}
              </p>
            </div>
          )}

          {events.length === 0 ? (
            <p className="px-4 sm:px-5 py-8 text-center text-sm text-slate-400">
              Aucune annonce à fort impact prévue pour les devises suivies.
            </p>
          ) : (
            <ol className="relative max-h-[60dvh] space-y-1 overflow-y-auto overscroll-contain px-4 py-4 sm:px-5 lg:max-h-[36rem]">
              <span className="absolute top-6 bottom-6 left-[1.6rem] w-px bg-line" aria-hidden />
              {events.map((event) => {
                const style = STATUS[event.status];
                return (
                  <li
                    key={`${event.time}-${event.currency}-${event.title}`}
                    className={`relative flex gap-4 rounded-md py-2 pr-2 ${event.status === "passed" ? "opacity-60" : ""}`}
                  >
                    <span className={`relative z-10 mt-1.5 size-2.5 shrink-0 translate-x-[0.1875rem] rounded-full ${style.dot}`} aria-hidden />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-sm font-semibold text-white tabular-nums">
                          {formatClock(event.time)}
                        </span>
                        <span className="text-xs text-slate-500">
                          {formatDayIfNotToday(event.time)} {timeAgo(event.time)}
                        </span>
                        <span className={`rounded-md px-1.5 py-0.5 text-[11px] font-semibold ${style.badge}`}>{style.label}</span>
                        {event.blocks_trading && (
                          <span className="rounded-md bg-rose-600 px-1.5 py-0.5 text-[11px] font-bold text-white">BLOQUE</span>
                        )}
                      </div>
                      <p className="mt-1 text-sm text-slate-200">
                        <span className="mr-1.5 rounded bg-line px-1.5 py-0.5 font-mono text-[11px] font-bold text-slate-100">
                          {event.currency}
                        </span>
                        {event.title}
                      </p>
                      {(event.forecast || event.previous || event.symbols.length > 0) && (
                        <p className="mt-1 text-xs text-slate-400">
                          {event.forecast && <>Prévision <span className="font-mono text-slate-300">{event.forecast}</span> · </>}
                          {event.previous && <>Précédent <span className="font-mono text-slate-300">{event.previous}</span> · </>}
                          {event.symbols.length > 0 && <>Exposés : {event.symbols.join(", ")}</>}
                        </p>
                      )}
                    </div>
                  </li>
                );
              })}
            </ol>
          )}
        </>
      )}
    </GlassCard>
  );
}
