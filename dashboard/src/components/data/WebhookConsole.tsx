import { Check, Copy, SquareTerminal } from "lucide-react";
import { useState, type ReactNode } from "react";
import type { WebhookLog } from "../../api/types";
import { formatClock, formatDayIfNotToday } from "../../lib/format";
import { GlassCard, Skeleton } from "../ui/GlassCard";

// Clés, chaînes, booléens/null, nombres
const JSON_TOKEN = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;

/** JSON coloré, rendu en éléments React (aucun HTML injecté : le contenu vient de l'extérieur). */
function JsonView({ value }: { value: unknown }) {
  if (typeof value === "string") {
    return <pre className="break-all whitespace-pre-wrap text-slate-300">{value || "(corps vide)"}</pre>;
  }
  const text = JSON.stringify(value, null, 2) ?? String(value);
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(JSON_TOKEN)) {
    const index = match.index ?? 0;
    if (index > last) parts.push(text.slice(last, index));
    const [token, str, colon, literal] = match;
    const className = str ? (colon ? "text-sky-300" : "text-emerald-300") : literal ? "text-violet-300" : "text-amber-300";
    parts.push(<span key={index} className={className}>{token}</span>);
    last = index + token.length;
  }
  parts.push(text.slice(last));
  return <pre className="break-all whitespace-pre-wrap text-slate-400">{parts}</pre>;
}

function CopyButton({ text }: { text: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text); // indisponible hors HTTPS / localhost
      setState("copied");
    } catch {
      setState("failed");
    }
    window.setTimeout(() => setState("idle"), 2_000);
  };
  return (
    <button
      type="button"
      onClick={() => void copy()}
      className="inline-flex h-9 shrink-0 items-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-2.5 text-xs text-slate-300 hover:bg-white/10"
    >
      {state === "copied" ? <Check className="size-3.5 text-emerald-400" /> : <Copy className="size-3.5" />}
      {state === "copied" ? "Copié" : state === "failed" ? "Sélectionnez" : "Copier"}
    </button>
  );
}

const EXAMPLE_MESSAGE = `{
  "symbol": "{{ticker}}",
  "action": "{{strategy.order.action}}",
  "price": {{close}},
  "passphrase": "<WEBHOOK_SECRET>"
}`;

interface WebhookConsoleProps {
  log: WebhookLog | null;
  baseUrl: string;
}

/** Console de débogage des webhooks entrants (TradingView...) : derniers signaux et JSON brut. */
export function WebhookConsole({ log, baseUrl }: WebhookConsoleProps) {
  const endpoint = `${baseUrl}/api/webhooks/tradingview?token=<WEBHOOK_SECRET>`;

  return (
    <GlassCard
      title="Webhooks entrants"
      icon={<SquareTerminal className="size-4" />}
      actions={
        log && (
          <span className={`text-xs font-semibold ${log.enabled ? "text-emerald-300" : "text-slate-500"}`}>
            {log.enabled ? `Actif · ${log.events.length}/${log.capacity}` : "Désactivé"}
          </span>
        )
      }
    >
      <details className="group border-b border-white/5 px-4 py-3 text-xs text-slate-400 sm:px-5">
        <summary className="flex min-h-9 cursor-pointer items-center font-semibold text-slate-300 select-none">
          Configurer TradingView
        </summary>
        <div className="mt-2 space-y-3">
          <p>
            Signaux <strong className="text-slate-200">journalisés uniquement, jamais exécutés</strong> : les ordres passent
            toujours par la stratégie et le gestionnaire de risque. TradingView n'envoie qu'aux ports 80/443 : l'API doit
            être publique en HTTPS (reverse proxy).
          </p>
          {!log?.enabled && (
            <p className="text-amber-300">Définissez WEBHOOK_SECRET dans le .env du bot pour activer la réception.</p>
          )}
          <div>
            <p className="mb-1 text-slate-500">URL du webhook</p>
            <div className="flex items-start gap-2">
              <code className="min-w-0 flex-1 rounded-lg bg-black/40 px-2 py-1.5 font-mono break-all text-slate-200">{endpoint}</code>
              <CopyButton text={endpoint} />
            </div>
          </div>
          <div>
            <p className="mb-1 text-slate-500">Message d'alerte (exemple)</p>
            <pre className="rounded-lg bg-black/40 px-2 py-1.5 font-mono text-slate-300">{EXAMPLE_MESSAGE}</pre>
          </div>
        </div>
      </details>

      <div className="m-3 rounded-xl border border-white/5 bg-black/50 font-mono text-xs sm:m-4">
        {log === null ? (
          <div className="space-y-2 p-3">
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        ) : log.events.length === 0 ? (
          <p className="p-3 text-slate-500">
            &gt; En attente de signaux<span className="animate-pulse">_</span>
          </p>
        ) : (
          <ol className="divide-y divide-white/5 lg:max-h-[28rem] lg:overflow-y-auto lg:overscroll-contain">
            {log.events.map((event, index) => (
              <li key={event.id}>
                <details open={index === 0}>
                  <summary className="flex min-h-11 cursor-pointer flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2 select-none hover:bg-white/5">
                    <span className="text-slate-500">
                      [{formatDayIfNotToday(event.received_at)} {formatClock(event.received_at, true)}]
                    </span>
                    <span className="text-slate-400">#{event.id}</span>
                    <span className="text-sky-300">{event.source}</span>
                    {event.action && (
                      <span className={/BUY|LONG/.test(event.action) ? "text-emerald-300" : /SELL|SHORT/.test(event.action) ? "text-rose-300" : "text-slate-200"}>
                        {event.action}
                      </span>
                    )}
                    {event.symbol && <span className="font-semibold text-white">{event.symbol}</span>}
                    <span className={`ml-auto ${event.accepted ? "text-emerald-400" : "text-rose-400"}`}>
                      {event.accepted ? "✓" : "✗"} {event.reason}
                    </span>
                  </summary>
                  <div className="px-3 pb-3">
                    <p className="mb-1 text-[11px] text-slate-600">
                      {event.client} · {event.size_bytes} o
                    </p>
                    <JsonView value={event.payload} />
                  </div>
                </details>
              </li>
            ))}
          </ol>
        )}
      </div>
    </GlassCard>
  );
}
