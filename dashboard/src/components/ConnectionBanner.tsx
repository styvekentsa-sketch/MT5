import { PlugZap, TriangleAlert, WifiOff } from "lucide-react";
import type { ApiError } from "../api/client";
import type { Status } from "../api/types";

interface ConnectionBannerProps {
  online: boolean;
  baseUrl: string;
  error: ApiError | null;
  status: Status | null;
}

/** Avertissements de connexion : appareil hors ligne, API injoignable, MT5 déconnecté. */
export function ConnectionBanner({ online, baseUrl, error, status }: ConnectionBannerProps) {
  if (!online) {
    return (
      <div className="flex items-start gap-3 rounded-md border border-slate-500/40 bg-slate-500/10 p-4 text-sm text-slate-200">
        <WifiOff className="mt-0.5 size-5 shrink-0" aria-hidden />
        <p>
          <span className="font-semibold">Appareil hors ligne.</span> Les données affichées sont celles du dernier
          rafraîchissement ; la mise à jour reprendra au retour du réseau.
        </p>
      </div>
    );
  }

  if (error?.kind === "network") {
    return (
      <div className="flex items-start gap-3 rounded-md border border-rose-500/40 bg-rose-500/10 p-4 text-sm text-rose-200">
        <PlugZap className="mt-0.5 size-5 shrink-0" aria-hidden />
        <div className="min-w-0">
          <p className="font-semibold text-rose-100">API déconnectée</p>
          <p className="break-words">
            {error.message}. Vérifiez que le bot est lancé avec <span className="font-mono">API_ENABLED=true</span>,
            que l'URL (<span className="font-mono">{baseUrl}</span>) est joignable depuis cet appareil et que son
            origine figure dans <span className="font-mono">API_CORS_ORIGINS</span>.
          </p>
        </div>
      </div>
    );
  }

  const warning =
    error?.kind === "unavailable" || error?.kind === "http"
      ? error.message
      : status && !status.mt5_connected
        ? !status.broker || status.broker === "mt5"
          ? "Le terminal MetaTrader 5 n'est pas connecté au broker : aucun ordre ne peut être passé."
          : `L'exchange ${status.broker} ne répond pas : aucun ordre ne peut être passé (reconnexion automatique en cours).`
        : null;
  if (!warning) return null;

  return (
    <div className="flex items-start gap-3 rounded-md border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-200">
      <TriangleAlert className="mt-0.5 size-5 shrink-0" aria-hidden />
      <p>{warning}</p>
    </div>
  );
}
