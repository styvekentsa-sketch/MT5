import { useCallback, useState } from "react";
import { api, errorMessage, type ApiError, type Connection } from "./api/client";
import { AccountOverview } from "./components/AccountOverview";
import { ConnectionBanner } from "./components/ConnectionBanner";
import { ControlPanel } from "./components/ControlPanel";
import { CycleOutcomes } from "./components/CycleOutcomes";
import { MobileNav, type Tab } from "./components/MobileNav";
import { PositionsTable } from "./components/PositionsTable";
import { SentimentMonitor } from "./components/SentimentMonitor";
import { SettingsDialog } from "./components/SettingsDialog";
import { Toasts } from "./components/ui/Toasts";
import { useConnection } from "./hooks/useConnection";
import { useInstallPrompt, useOnlineStatus } from "./hooks/useDevice";
import { usePolling } from "./hooks/usePolling";
import { useToasts } from "./hooks/useToasts";

const LIVE_POLL_MS = 2_500;
// Les actualités sont rafraîchies côté bot toutes les NEWS_REFRESH_INTERVAL_SECONDS (300 s par défaut)
const SENTIMENT_POLL_MS = 60_000;

export default function App() {
  const { connection, save, forgetKey } = useConnection();
  const { toasts, push, dismiss } = useToasts();
  const installState = useInstallPrompt();
  const online = useOnlineStatus();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("account");
  const enabled = connection.apiKey !== "";

  const status = usePolling(
    useCallback((s: AbortSignal) => api.status(connection, s), [connection]),
    LIVE_POLL_MS,
    enabled,
  );
  const account = usePolling(
    useCallback((s: AbortSignal) => api.account(connection, s), [connection]),
    LIVE_POLL_MS,
    enabled,
  );
  const positions = usePolling(
    useCallback((s: AbortSignal) => api.positions(connection, s), [connection]),
    LIVE_POLL_MS,
    enabled,
  );
  const sentiment = usePolling(
    useCallback((s: AbortSignal) => api.sentiment(connection, s), [connection]),
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
  const { refresh: refreshSentiment } = sentiment;
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

  const handleSave = (next: Connection) => {
    save(next);
    setSettingsOpen(false);
    refreshLive();
    refreshSentiment();
  };

  const handleInstall = async () => {
    if (await installState.install()) push("success", "Application installée");
  };

  const changeTab = (next: Tab) => {
    setTab(next);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  // Mobile : un panneau à la fois (barre de navigation) ; à partir de `lg`, tout est visible.
  const panel = (id: Tab) => (tab === id ? "" : "hidden lg:block");

  return (
    <div className="min-h-dvh text-slate-100">
      <ControlPanel
        status={status.data}
        apiConnected={apiConnected}
        lastUpdated={status.lastUpdated}
        canInstall={installState.canInstall}
        onInstall={() => void handleInstall()}
        onOpenSettings={() => setSettingsOpen(true)}
        onStart={() =>
          perform("Démarrage", async () => {
            const result = await api.start(connection);
            if (!result.success) throw new Error(result.message);
            return result.message;
          })
        }
        onStop={() =>
          perform("Pause", async () => {
            const result = await api.stop(connection);
            if (!result.success) throw new Error(result.message);
            return result.message;
          })
        }
        onSetMode={(live) => perform("Changement de mode", async () => (await api.setMode(connection, live)).message)}
        onKillSwitch={(includeManual) =>
          perform("Kill switch", async () => {
            const result = await api.killSwitch(connection, includeManual);
            if (result.failed > 0) {
              throw new Error(`${result.failed} clôture(s) en échec, ${result.closed} réussie(s) : vérifiez le terminal MT5`);
            }
            return result.closed > 0
              ? `${result.closed} position(s) fermée(s), moteur en pause`
              : "Aucune position à fermer, moteur en pause";
          })
        }
      />

      <main className="px-safe pb-mobile-nav mx-auto flex max-w-7xl flex-col gap-4 pt-4 lg:gap-6 lg:pt-6 lg:pb-8">
        <ConnectionBanner online={online} baseUrl={connection.baseUrl} error={bannerError} status={status.data} />

        <div className={panel("account")}>
          <AccountOverview account={account.data} />
        </div>

        <div className="grid grid-cols-1 gap-6 xl:grid-cols-5">
          <div className="flex flex-col gap-6 xl:col-span-3">
            <div className={panel("positions")}>
              <PositionsTable
                positions={positions.data}
                currency={account.data?.currency ?? "USD"}
                disabled={!apiConnected}
                onClose={(ticket) =>
                  perform("Clôture", async () => {
                    const result = await api.closePosition(connection, ticket);
                    if (!result.success) throw new Error(result.message);
                    return `Position #${ticket} (${result.symbol}) fermée`;
                  })
                }
              />
            </div>
            <div className={panel("engine")}>
              <CycleOutcomes status={status.data} />
            </div>
          </div>
          <div className={`xl:col-span-2 ${panel("news")}`}>
            <SentimentMonitor sentiment={sentiment.data} lastUpdated={sentiment.lastUpdated} onRefresh={refreshSentiment} />
          </div>
        </div>

        <footer className="hidden text-xs text-slate-600 lg:block">
          API : <span className="font-mono">{connection.baseUrl}</span> · rafraîchissement {LIVE_POLL_MS / 1000} s
        </footer>
      </main>

      <MobileNav active={tab} positionCount={positions.data?.length ?? 0} onChange={changeTab} />

      <SettingsDialog
        open={!enabled || unauthorized || settingsOpen}
        connection={connection}
        dismissable={enabled && !unauthorized}
        error={unauthorized ? "Clé API refusée par le serveur : vérifiez API_KEY dans le .env du bot." : null}
        installState={installState}
        onSave={handleSave}
        onForgetKey={() => {
          forgetKey();
          setSettingsOpen(false);
        }}
        onClose={() => setSettingsOpen(false)}
      />
      <Toasts toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
