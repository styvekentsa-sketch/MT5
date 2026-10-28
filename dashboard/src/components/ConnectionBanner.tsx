import { PlugZap, TriangleAlert } from "lucide-react";
import { API_URL, type ApiError } from "../api/client";
import type { Status } from "../api/types";

interface ConnectionBannerProps {
  error: ApiError | null;
  status: Status | null;
}

/** Avertissements de connexion : API injoignable, MT5 déconnecté, service indisponible. */
export function ConnectionBanner({ error, status }: ConnectionBannerProps) {
  if (error?.kind === "network") {
    return (
      <div className="flex items-start gap-3 rounded-2xl border border-rose-500/40 bg-rose-500/10 p-4 text-sm text-rose-200">
        <PlugZap className="mt-0.5 size-5 shrink-0" aria-hidden />
        <div>
          <p className="font-semibold text-rose-100">API déconnectée</p>
          <p>
            Impossible de joindre <span className="font-mono">{API_URL}</span>. Vérifiez que le bot est lancé
            (<span className="font-mono">python main.py</span>) avec <span className="font-mono">API_ENABLED=true</span>.
            Les données affichées peuvent être périmées.
          </p>
        </div>
      </div>
    );
  }

  const warning =
    error?.kind === "unavailable" || error?.kind === "http"
      ? error.message
      : status && !status.mt5_connected
        ? "Le terminal MetaTrader 5 n'est pas connecté au broker : aucun ordre ne peut être passé."
        : null;
  if (!warning) return null;

  return (
    <div className="flex items-start gap-3 rounded-2xl border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-200">
      <TriangleAlert className="mt-0.5 size-5 shrink-0" aria-hidden />
      <p>{warning}</p>
    </div>
  );
}
