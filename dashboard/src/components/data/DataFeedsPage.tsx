import { DatabaseZap, RefreshCw } from "lucide-react";
import type { DataFeeds, WebhookLog } from "../../api/types";
import { formatTime } from "../../lib/format";
import { CalendarTimeline } from "./CalendarTimeline";
import { FeedLog } from "./FeedLog";
import { SourcesStrip } from "./SourcesStrip";
import { WebhookConsole } from "./WebhookConsole";

interface DataFeedsPageProps {
  feeds: DataFeeds | null;
  webhooks: WebhookLog | null;
  baseUrl: string;
  lastUpdated: Date | null;
  onRefresh: () => void;
}

/**
 * « Data & Scraper Monitor » : traçabilité de tout ce que le bot lit (flux RSS, calendrier,
 * webhooks). Mobile : calendrier, flux puis webhooks ; desktop : flux à gauche, le reste à droite.
 */
export function DataFeedsPage({ feeds, webhooks, baseUrl, lastUpdated, onRefresh }: DataFeedsPageProps) {
  return (
    <section className="flex flex-col gap-4 lg:gap-6" aria-labelledby="data-feeds-title">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 id="data-feeds-title" className="flex items-center gap-2 text-lg font-semibold text-white">
            <DatabaseZap className="size-5 text-sky-400" aria-hidden />
            Données ingérées
          </h2>
          <p className="text-sm text-slate-400">Tout ce que le bot lit sur Internet et reçoit par webhook.</p>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          className="inline-flex h-11 shrink-0 items-center gap-1.5 rounded-md border border-line bg-surface-2 px-3 text-xs text-slate-300 hover:bg-line"
          title="Rafraîchir"
        >
          <RefreshCw className="size-4" aria-hidden />
          {formatTime(lastUpdated)}
        </button>
      </div>

      <SourcesStrip sources={feeds?.sources ?? null} />

      <div className="grid grid-cols-1 items-start gap-4 lg:gap-6 xl:grid-cols-5">
        <div className="order-2 xl:order-none xl:col-span-3 xl:row-span-2">
          <FeedLog news={feeds?.news ?? null} calendar={feeds?.calendar ?? null} symbols={feeds?.tracked_symbols ?? []} />
        </div>
        <div className="order-1 xl:order-none xl:col-span-2">
          <CalendarTimeline events={feeds?.calendar ?? null} blockWindow={feeds?.block_window_minutes ?? null} />
        </div>
        <div className="order-3 xl:order-none xl:col-span-2">
          <WebhookConsole log={webhooks} baseUrl={baseUrl} />
        </div>
      </div>
    </section>
  );
}
