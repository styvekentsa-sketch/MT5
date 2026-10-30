import { useCallback, useEffect, useMemo, useState } from "react";
import { api, errorMessage, scopedConnection, type ApiError, type Connection } from "./api/client";
import { AccountOverview } from "./components/AccountOverview";
import { AccountsBar } from "./components/AccountsBar";
import { ConnectionBanner } from "./components/ConnectionBanner";
import { ControlPanel } from "./components/ControlPanel";
import { CycleOutcomes } from "./components/CycleOutcomes";
import { DecisionJournal, type DecisionFilter } from "./components/DecisionJournal";
import { DataFeedsPage } from "./components/data/DataFeedsPage";
import { MobileNav, type Tab } from "./components/MobileNav";
import { PriceChart } from "./components/Chart";
import { PositionsTable } from "./components/PositionsTable";
import { SentimentMonitor } from "./components/SentimentMonitor";
import { SettingsDialog } from "./components/SettingsDialog";
import { Toasts } from "./components/ui/Toasts";
import { useConnection } from "./hooks/useConnection";
import { useInstallPrompt, useMediaQuery, useOnlineStatus } from "./hooks/useDevice";
import { usePolling } from "./hooks/usePolling";
import { useToasts } from "./hooks/useToasts";

const LIVE_POLL_MS = 2_500;
// Compte et positions : chaque seconde, pour suivre le P/L des micro-trades au centime près
const PNL_POLL_MS = 1_000;
// Les actualités sont rafraîchies côté bot toutes les NEWS_REFRESH_INTERVAL_SECONDS (300 s par défaut)
const SENTIMENT_POLL_MS = 60_000;
const ACCOUNTS_POLL_MS = 5_000;
const SELECTED_ACCOUNT_KEY = "mt5bot.account";
// Page « Données » : interrogée uniquement quand elle est affichée
const DATA_FEEDS_POLL_MS = 30_000;
const WEBHOOKS_POLL_MS = 5_000;
// Journal des décisions : suit le moteur tour par tour (interrogé seulement quand le panneau est affiché)
const DECISIONS_POLL_MS = 5_000;
const DECISIONS_LIMIT = 50;

const VIEW_TABS = [
  { label: "Trading", tab: "account" },
  { label: "Données", tab: "data" },
] as const;

export default function App() {
  const { connection, save, forgetKey } = useConnection();
  const { toasts, push, dismiss } = useToasts();
  const installState = useInstallPrompt();
  const online = useOnlineStatus();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("account");
  const enabled = connection.apiKey !== "";

  // Multi-comptes : la passerelle liste les comptes ; une API de compte unique répond 404 (pas de sélecteur)
  const [accountsUnsupported, setAccountsUnsupported] = useState(false);
  const accountsPoll = usePolling(
    useCallback((s: AbortSignal) => api.accounts(connection, s), [connection]),
    ACCOUNTS_POLL_MS,
    enabled && !accountsUnsupported,
    connection.baseUrl,
  );
  useEffect(() => {
    if (accountsPoll.error?.status === 404) setAccountsUnsupported(true);
  }, [accountsPoll.error]);
  useEffect(() => setAccountsUnsupported(false), [connection]);
  const accountList = accountsPoll.data?.accounts ?? [];
  const multiAccount = accountList.length > 0;
  const [selectedAccount, setSelectedAccount] = useState<string>(() => {
    try {
      return localStorage.getItem(SELECTED_ACCOUNT_KEY) ?? "";
    } catch {
      return "";
    }
  });
  useEffect(() => {
    if (multiAccount && !accountList.some((a) => a.id === selectedAccount)) setSelectedAccount(accountList[0].id);
  }, [multiAccount, accountList, selectedAccount]);
  const selectAccount = (id: string) => {
    setSelectedAccount(id);
    try {
      localStorage.setItem(SELECTED_ACCOUNT_KEY, id);
    } catch {
      // mémorisation facultative
    }
  };
  const activeAccount = accountList.find((a) => a.id === selectedAccount);
  // Toutes les données et actions visent le compte sélectionné (ou l'API du compte unique)
  // Dépend de l'identifiant (stable) et non de l'objet compte, renouvelé à chaque rafraîchissement :
  // sinon le graphique rechargerait tout son historique toutes les 5 s.
  const activeAccountId = multiAccount ? activeAccount?.id : undefined;
  const scoped = useMemo(
    () => (activeAccountId ? scopedConnection(connection, activeAccountId) : connection),
    [activeAccountId, connection],
  );

  const status = usePolling(
    useCallback((s: AbortSignal) => api.status(scoped, s), [scoped]),
    LIVE_POLL_MS,
    enabled,
    scoped.baseUrl,
  );
  const account = usePolling(
    useCallback((s: AbortSignal) => api.account(scoped, s), [scoped]),
    PNL_POLL_MS,
    enabled,
    scoped.baseUrl,
  );
  const positions = usePolling(
    useCallback((s: AbortSignal) => api.positions(scoped, s), [scoped]),
    PNL_POLL_MS,
    enabled,
    scoped.baseUrl,
  );
  const sentiment = usePolling(
    useCallback((s: AbortSignal) => api.sentiment(scoped, s), [scoped]),
    SENTIMENT_POLL_MS,
    enabled,
    scoped.baseUrl,
  );
  const onDataView = tab === "data";
  // À partir de la tablette (md), tous les panneaux de la vue sont affichés ensemble
  const isWide = useMediaQuery("(min-width: 768px)");
  const [decisionFilter, setDecisionFilter] = useState<DecisionFilter>("all");
  const decisions = usePolling(
    useCallback(
      (s: AbortSignal) => api.botDecisions(scoped, decisionFilter, DECISIONS_LIMIT, s),
      [scoped, decisionFilter],
    ),
    DECISIONS_POLL_MS,
    enabled && !onDataView && (isWide || tab === "engine"),
    `${scoped.baseUrl}|${decisionFilter}`,
  );
  const dataFeeds = usePolling(
    useCallback((s: AbortSignal) => api.dataFeeds(scoped, s), [scoped]),
    DATA_FEEDS_POLL_MS,
    enabled && onDataView,
    scoped.baseUrl,
  );
  const webhookLog = usePolling(
    useCallback((s: AbortSignal) => api.webhookEvents(scoped, s), [scoped]),
    WEBHOOKS_POLL_MS,
    enabled && onDataView,
    scoped.baseUrl,
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

  // Une position s'ouvre ou se ferme : solde et P/L du jour relus aussitôt (sans attendre le tour suivant)
  const openTickets = positions.data?.map((p) => p.ticket).sort((x, y) => x - y).join(",");
  useEffect(() => {
    if (openTickets !== undefined) refreshAccount();
  }, [openTickets, refreshAccount]);

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

  // Mobile (< md) : un panneau à la fois (barre de navigation). À partir de `md` : vue « Trading »
  // (tous les panneaux) ou vue « Données », selon les onglets du haut.
  const panel = (id: Tab) => (tab === id ? "" : onDataView ? "hidden" : "hidden md:block");
  const { refresh: refreshDataFeeds } = dataFeeds;
  const { refresh: refreshWebhooks } = webhookLog;

  return (
    <div className="min-h-dvh w-full overflow-x-clip text-slate-100">
      <ControlPanel
        status={status.data}
        apiConnected={apiConnected}
        lastUpdated={status.lastUpdated}
        canInstall={installState.canInstall}
        onInstall={() => void handleInstall()}
        onOpenSettings={() => setSettingsOpen(true)}
        accountLabel={multiAccount ? activeAccount?.label : undefined}
        onStart={() =>
          perform("Démarrage", async () => {
            const result = await api.start(scoped);
            if (!result.success) throw new Error(result.message);
            return result.message;
          })
        }
        onStop={() =>
          perform("Pause", async () => {
            const result = await api.stop(scoped);
            if (!result.success) throw new Error(result.message);
            return result.message;
          })
        }
        onSetMode={(live) => perform("Changement de mode", async () => (await api.setMode(scoped, live)).message)}
        onKillSwitch={(includeManual) =>
          perform("Kill switch", async () => {
            const result = await api.killSwitch(scoped, includeManual);
            if (result.failed > 0) {
              throw new Error(`${result.failed} clôture(s) en échec, ${result.closed} réussie(s) : vérifiez le terminal MT5`);
            }
            return result.closed > 0
              ? `${result.closed} position(s) fermée(s), moteur en pause`
              : "Aucune position à fermer, moteur en pause";
          })
        }
      />

      <main className="px-safe pb-mobile-nav mx-auto flex w-full max-w-7xl min-w-0 flex-col gap-4 pt-4 md:pb-8 lg:gap-6 lg:pt-6 2xl:max-w-[100rem]">
        <ConnectionBanner online={online} baseUrl={connection.baseUrl} error={bannerError} status={status.data} />

        {multiAccount && <AccountsBar accounts={accountList} selected={selectedAccount} onSelect={selectAccount} />}

        <nav className="hidden gap-1 self-start rounded-md border border-line bg-surface-2 p-1 md:flex" aria-label="Vues">
          {VIEW_TABS.map(({ label, tab: target }) => {
            const active = (target === "data") === onDataView;
            return (
              <button
                key={target}
                type="button"
                onClick={() => setTab(target)}
                aria-current={active ? "page" : undefined}
                className={`h-10 rounded-lg px-4 text-sm font-semibold transition ${
                  active ? "bg-sky-500/90 text-white" : "text-slate-300 hover:bg-line"
                }`}
              >
                {label}
              </button>
            );
          })}
        </nav>

        {onDataView && (
          <DataFeedsPage
            feeds={dataFeeds.data}
            webhooks={webhookLog.data}
            baseUrl={connection.baseUrl}
            lastUpdated={dataFeeds.lastUpdated}
            onRefresh={() => {
              refreshDataFeeds();
              refreshWebhooks();
            }}
          />
        )}

        <div className={panel("account")}>
          <AccountOverview account={account.data} connected={apiConnected ? (status.data?.mt5_connected ?? null) : false} />
        </div>

        {/*
          Mobile : une colonne. Tablette (md) : graphique pleine largeur, puis positions + moteur
          à gauche et actualités à droite. Bureau (xl) : graphique, positions et moteur sur 3/5,
          actualités sur 2/5 de toute la hauteur (disposition station de trading).
        */}
        <div className={`grid grid-cols-1 items-start gap-4 md:grid-cols-2 lg:gap-6 xl:grid-cols-5 ${onDataView ? "hidden" : ""}`}>
            <div className={`min-w-0 md:col-span-2 xl:col-span-3 xl:col-start-1 xl:row-start-1 ${panel("positions")}`}>
              <PriceChart
                connection={scoped}
                symbols={status.data?.symbols ?? []}
                positions={positions.data}
                active={enabled && !onDataView && (isWide || tab === "positions")}
              />
            </div>
            <div className={`min-w-0 md:col-start-1 md:row-start-2 xl:col-span-3 ${panel("positions")}`}>
              <PositionsTable
                positions={positions.data}
                currency={account.data?.currency ?? "USD"}
                balance={account.data?.balance ?? null}
                disabled={!apiConnected}
                onClose={(ticket) =>
                  perform("Clôture", async () => {
                    const result = await api.closePosition(scoped, ticket);
                    if (!result.success) throw new Error(result.message);
                    return `Position #${ticket} (${result.symbol}) fermée`;
                  })
                }
              />
            </div>
            <div className={`min-w-0 md:col-start-1 md:row-start-3 xl:col-span-3 ${panel("engine")}`}>
              <div className="flex flex-col gap-4 lg:gap-6">
                <CycleOutcomes status={status.data} />
                <DecisionJournal
                  journal={decisions.data}
                  filter={decisionFilter}
                  onFilterChange={setDecisionFilter}
                  lastUpdated={decisions.lastUpdated}
                />
              </div>
            </div>
          <div
            className={`min-w-0 md:col-start-2 md:row-span-2 md:row-start-2 xl:col-span-2 xl:col-start-4 xl:row-span-3 xl:row-start-1 ${panel("news")}`}
          >
            <SentimentMonitor sentiment={sentiment.data} lastUpdated={sentiment.lastUpdated} onRefresh={refreshSentiment} />
          </div>
        </div>

        <footer className="hidden text-xs text-slate-600 md:block">
          API : <span className="font-mono">{connection.baseUrl}</span> · rafraîchissement {PNL_POLL_MS / 1000} s (compte, positions) · {LIVE_POLL_MS / 1000} s (moteur)
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
