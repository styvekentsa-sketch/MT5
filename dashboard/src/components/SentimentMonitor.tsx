import { ExternalLink, Minus, Newspaper, RefreshCw, TrendingDown, TrendingUp } from "lucide-react";
import type { SentimentLabel, Sentiment } from "../api/types";
import { formatSigned, formatTime, isSafeUrl, signColor, timeAgo } from "../lib/format";
import { GlassCard, Skeleton } from "./ui/GlassCard";

const LABELS: Record<SentimentLabel, { text: string; className: string; Icon: typeof Minus }> = {
  bullish: { text: "Bullish", className: "bg-emerald-500/15 text-emerald-300", Icon: TrendingUp },
  bearish: { text: "Bearish", className: "bg-rose-500/15 text-rose-300", Icon: TrendingDown },
  neutral: { text: "Neutre", className: "bg-slate-500/20 text-slate-300", Icon: Minus },
};

function LabelBadge({ label, score }: { label: SentimentLabel; score?: number }) {
  const { text, className, Icon } = LABELS[label];
  return (
    <span className={`inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-0.5 text-xs font-semibold ${className}`}>
      <Icon className="size-3" aria-hidden />
      {text}
      {score !== undefined && <span className="font-mono tabular-nums opacity-80">{formatSigned(score)}</span>}
    </span>
  );
}

/** Jauge horizontale d'un score de -1 (baissier) à +1 (haussier). */
function ScoreGauge({ score }: { score: number }) {
  const position = ((Math.max(-1, Math.min(1, score)) + 1) / 2) * 100;
  return (
    <div className="relative h-1.5 rounded-sm bg-linear-to-r from-rose-500/60 via-slate-700 to-emerald-500/60">
      <div
        className="absolute top-1/2 size-4 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-slate-950 bg-white transition-[left] duration-700"
        style={{ left: `${position}%` }}
        aria-hidden
      />
    </div>
  );
}

interface SentimentMonitorProps {
  sentiment: Sentiment | null;
  lastUpdated: Date | null;
  onRefresh: () => void;
}

/** Sentiment global, sentiment orienté par paire et flux des dernières actualités. */
export function SentimentMonitor({ sentiment, lastUpdated, onRefresh }: SentimentMonitorProps) {
  return (
    <GlassCard
      title="Sentiment des actualités"
      icon={<Newspaper className="size-4" />}
      className="flex flex-col"
      actions={
        <button
          type="button"
          onClick={onRefresh}
          className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs text-slate-400 hover:bg-line hover:text-white"
          title="Rafraîchir"
        >
          <RefreshCw className="size-3.5" aria-hidden />
          {formatTime(lastUpdated)}
        </button>
      }
    >
      {!sentiment ? (
        <div className="space-y-3 p-5">
          <Skeleton className="h-4 w-1/2" />
          <Skeleton className="h-2 w-full" />
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      ) : (
        <>
          <div className="space-y-4 border-b border-line p-5">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs tracking-wide text-slate-400 uppercase">Sentiment global</p>
                <p className={`font-mono text-2xl font-semibold tabular-nums ${signColor(sentiment.global_score)}`}>
                  {formatSigned(sentiment.global_score)}
                </p>
              </div>
              <div className="text-right">
                <LabelBadge label={sentiment.global_label} />
                <p className="mt-1 text-xs text-slate-500">
                  {sentiment.article_count} articles · moteur {sentiment.engine}
                </p>
              </div>
            </div>
            <ScoreGauge score={sentiment.global_score} />

            <div className="flex flex-wrap gap-2">
              {Object.entries(sentiment.by_symbol).map(([symbol, score]) => (
                <span
                  key={symbol}
                  className="inline-flex items-center gap-2 rounded-lg border border-line bg-surface-2 px-2.5 py-1 text-xs"
                  title="Sentiment orienté pour la paire (> 0 = haussier)"
                >
                  <span className="font-semibold text-slate-200">{symbol}</span>
                  <span className={`font-mono tabular-nums ${signColor(score)}`}>{formatSigned(score)}</span>
                </span>
              ))}
            </div>
          </div>

          {sentiment.articles.length === 0 ? (
            <p className="px-5 py-10 text-center text-sm text-slate-400">Aucune actualité récente.</p>
          ) : (
            <ul className="divide-y divide-line lg:max-h-[32rem] lg:overflow-y-auto lg:overscroll-contain">
              {sentiment.articles.map((article) => (
                <li key={article.link} className="px-5 py-3 transition hover:bg-surface-2">
                  <div className="flex items-start justify-between gap-3">
                    {isSafeUrl(article.link) ? (
                      <a
                        href={article.link}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="group text-sm font-medium text-slate-100 hover:text-sky-300"
                      >
                        {article.title}
                        <ExternalLink className="ml-1 inline size-3 opacity-0 transition group-hover:opacity-100" aria-hidden />
                      </a>
                    ) : (
                      <span className="text-sm font-medium text-slate-100">{article.title}</span>
                    )}
                    <LabelBadge label={article.label} score={article.score} />
                  </div>
                  {article.snippet && <p className="mt-1 line-clamp-2 text-xs text-slate-400">{article.snippet}</p>}
                  <p className="mt-1 text-xs text-slate-500">
                    {article.source} · {timeAgo(article.published)}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </GlassCard>
  );
}
