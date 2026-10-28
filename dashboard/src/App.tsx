import { useCallback, useState } from "react";
import { api, API_URL, errorMessage, type ApiError } from "./api/client";
import { AccountOverview } from "./components/AccountOverview";
import { ApiKeyDialog } from "./components/ApiKeyDialog";
import { ConnectionBanner } from "./components/ConnectionBanner";
import { ControlPanel } from "./components/ControlPanel";
import { CycleOutcomes } from "./components/CycleOutcomes";
import { PositionsTable } from "./components/PositionsTable";
import { SentimentMonitor } from "./components/SentimentMonitor";
import { Toasts } from "./components/ui/Toasts";
import { useApiKey } from "./hooks/useApiKey";
import { usePolling } from "./hooks/usePolling";
import { useToasts } from "./hooks/useToasts";

const LIVE_POLL_MS = 2_500;
// Les actualités sont rafraîchies côté bot toutes les NEWS_REFRESH_INTERVAL_SECONDS (300 s par défaut)
const SENTIMENT_POLL_MS = 60_000;

export default function App() {
  const { apiKey, saveKey, clearKey } = useApiKey();
  const { toasts, push, dismiss } = useToasts();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const enabled = apiKey !== "";

  const status = usePolling(useCallback((s: AbortSignal) => api.status(apiKey, s), [apiKey]), LIVE_POLL_MS, enabled);
  const account = usePolling(useCallback((s: AbortSignal) => api.account(apiKey, s), [apiKey]), LIVE_POLL_MS, enabled);
  const positions = usePolling(
    useCallback((s: AbortSignal) => api.positions(apiKey, s), [apiKey]),
    LIVE_POLL_MS,
    enabled,
  );
  const sentiment = usePolling(
    useCallback((s: AbortSignal) => api.sentiment(apiKey, s), [apiKey]),
    SENTIMENT_POLL_MS,
    enabled,
  );

  const errors = [status.error, account.error, positions.error];
  const unauthorized = errors.some((e) => e?.kind === "unauthorized");
  const bannerError: ApiError | null =
    errors.find((e) => e?.kind === "network") ?? errors.find((e) => e && e.kind !== "unauthorized") ?? null;
  const apiConnected = enabled && status.data !== null && status.error === null;

  const { refresh: refreshStatus } = status;
  const { refresh: refreshAccount } = account;
  const { refresh: refreshPositions } = positions;
  const refreshLive = useCallback(() => {
    refreshStatus();
    refreshAccount();
    refreshPositions();
  }, [refreshStatus, refreshAccount, refreshPositions]);

  /** Exécute une action de contrôle : toast de succès ou d'erreur, puis rafraîchissement immédiat. */
  const perform = useCallback(
    async (label: string, action: () => Promise<string>) => {
      try {
        push("success", await action());
      } catch (error) {
        push("error", `${label} : ${errorMessage(error)}`);
      } finally {
        refreshLive();
      }
    },
    [push, refreshLive],
  );

  const handleSaveKey = (key: string) => {
    saveKey(key);
    setSettingsOpen(false);
    refreshLive();
    sentiment.refresh();
  };

  return (
    <div className="min-h-screen text-slate-100">
      <ControlPanel
        status={status.data}
        apiConnected={apiConnected}
        lastUpdated={status.lastUpdated}
        onOpenSettings={() => setSettingsOpen(true)}
        onStart={() =>
          perform("Démarrage", async () => {
            const result = await api.start(apiKey);
            if (!result.success) throw new Error(result.message);
            return result.message;
          })
        }
        onStop={() =>
          perform("Pause", async () => {
            const result = await api.stop(apiKey);
            if (!result.success) throw new Error(result.message);
            return result.message;
          })
        }
        onSetMode={(live) => perform("Changement de mode", async () => (await api.setMode(apiKey, live)).message)}
        onKillSwitch={(includeManual) =>
          perform("Kill switch", async () => {
            const result = await api.killSwitch(apiKey, includeManual);
            if (result.failed > 0) {
              throw new Error(`${result.failed} clôture(s) en échec, ${result.closed} réussie(s) : vérifiez le terminal MT5`);
            }
            return result.closed > 0
              ? `${result.closed} position(s) fermée(s), moteur en pause`
              : "Aucune position à fermer, moteur en pause";
          })
        }
      />

      <main className="mx-auto max-w-7xl space-y-6 px-4 py-6">
        <ConnectionBanner error={bannerError} status={status.data} />
        <AccountOverview account={account.data} />

        <div className="grid grid-cols-1 gap-6 xl:grid-cols-5">
          <div className="space-y-6 xl:col-span-3">
            <PositionsTable
              positions={positions.data}
              currency={account.data?.currency ?? "USD"}
              disabled={!apiConnected}
              onClose={(ticket) =>
                perform("Clôture", async () => {
                  const result = await api.closePosition(apiKey, ticket);
                  if (!result.success) throw new Error(result.message);
                  return `Position #${ticket} (${result.symbol}) fermée`;
                })
              }
            />
            <CycleOutcomes status={status.data} />
          </div>
          <div className="xl:col-span-2">
            <SentimentMonitor
              sentiment={sentiment.data}
              lastUpdated={sentiment.lastUpdated}
              onRefresh={sentiment.refresh}
            />
          </div>
        </div>
      </main>

      <footer className="mx-auto max-w-7xl px-4 pb-8 text-xs text-slate-600">
        API : <span className="font-mono">{API_URL}</span> · rafraîchissement {LIVE_POLL_MS / 1000} s
      </footer>

      <ApiKeyDialog
        open={!enabled || unauthorized || settingsOpen}
        hasKey={enabled}
        error={unauthorized ? "Clé API refusée par le serveur : vérifiez API_KEY dans le .env du bot." : null}
        onSave={handleSaveKey}
        onClear={() => {
          clearKey();
          setSettingsOpen(false);
        }}
        onCancel={() => setSettingsOpen(false)}
      />
      <Toasts toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
