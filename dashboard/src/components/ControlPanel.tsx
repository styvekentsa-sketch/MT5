import { Activity, Bot, Download, Pause, Play, Power, Settings } from "lucide-react";
import { useState } from "react";
import type { Mode, Status } from "../api/types";
import { formatTime, timeAgo } from "../lib/format";
import { Button } from "./ui/Button";
import { ConfirmDialog } from "./ui/ConfirmDialog";

interface ControlPanelProps {
  status: Status | null;
  apiConnected: boolean;
  lastUpdated: Date | null;
  canInstall: boolean;
  onInstall: () => void;
  onStart: () => Promise<void>;
  onStop: () => Promise<void>;
  onSetMode: (live: boolean) => Promise<void>;
  onKillSwitch: (includeManual: boolean) => Promise<void>;
  onOpenSettings: () => void;
  /** Multi-comptes : compte visé par toutes les actions de ce panneau */
  accountLabel?: string;
}

type BusyAction = "start" | "stop" | "mode" | "kill";

const STATE_STYLES = {
  running: { label: "En marche", dot: "bg-emerald-400", text: "text-emerald-300" },
  paused: { label: "En pause", dot: "bg-amber-400", text: "text-amber-300" },
  stopped: { label: "Arrêté", dot: "bg-slate-500", text: "text-slate-400" },
} as const;

function StatePill({ status }: { status: Status | null }) {
  const style = status ? STATE_STYLES[status.state] : null;
  return (
    <span className="inline-flex items-center gap-2 rounded border border-line bg-surface-2 px-2.5 py-1 text-xs font-medium">
      <span className={`size-2 rounded-full ${style?.dot ?? "bg-slate-600"}`} aria-hidden />
      <span className={style?.text ?? "text-slate-500"}>{style?.label ?? "Inconnu"}</span>
    </span>
  );
}

function ModePill({ mode }: { mode: Mode | undefined }) {
  if (!mode) return null;
  const live = mode === "LIVE";
  return (
    <span
      className={`inline-flex items-center rounded px-2.5 py-1 font-mono text-[11px] font-medium tracking-wider ${
        live
          ? "bg-rose-500/20 text-rose-300 ring-1 ring-rose-500/50"
          : "bg-emerald-500/15 text-emerald-300 ring-1 ring-emerald-500/40"
      }`}
    >
      {live ? "LIVE · argent réel" : "PAPER · simulation"}
    </span>
  );
}

/** Boutons secondaires (installer, paramètres) : 44 px, icône seule. */
function UtilityButtons({ canInstall, onInstall, onOpenSettings }: Pick<ControlPanelProps, "canInstall" | "onInstall" | "onOpenSettings">) {
  return (
    <>
      {canInstall && (
        <Button className="w-11 px-0" onClick={onInstall} aria-label="Installer l'application" title="Installer l'application">
          <Download className="size-4" />
        </Button>
      )}
      <Button className="w-11 px-0" onClick={onOpenSettings} aria-label="Paramètres de connexion" title="Paramètres de connexion">
        <Settings className="size-4" />
      </Button>
    </>
  );
}

/**
 * En-tête : statut du bot et commandes globales (start/pause, mode, kill-switch).
 * Mobile : commandes sur une grille de 3 boutons de 48 px, toujours visibles en haut.
 */
export function ControlPanel({
  status,
  apiConnected,
  lastUpdated,
  canInstall,
  onInstall,
  onStart,
  onStop,
  onSetMode,
  onKillSwitch,
  onOpenSettings,
  accountLabel,
}: ControlPanelProps) {
  const onAccount = accountLabel ? <> sur le compte <strong className="text-white">{accountLabel}</strong></> : null;
  const [busy, setBusy] = useState<BusyAction | null>(null);
  const [liveDialogOpen, setLiveDialogOpen] = useState(false);
  const [killDialogOpen, setKillDialogOpen] = useState(false);
  const [includeManual, setIncludeManual] = useState(false);

  const run = async (action: BusyAction, fn: () => Promise<void>) => {
    setBusy(action);
    try {
      await fn();
    } finally {
      setBusy(null);
    }
  };

  const controlsDisabled = !apiConnected || !status || busy !== null;
  const isLive = status?.mode === "LIVE";
  const canStart = status?.state === "paused" || status?.state === "stopped";
  const segment = "min-h-11 flex-1 rounded-lg px-2 text-xs font-bold transition disabled:cursor-not-allowed lg:flex-none lg:px-3";

  return (
    <header className="pt-safe sticky top-0 z-40 short:static border-b border-line bg-canvas/95">
      <div className="px-safe mx-auto flex max-w-7xl flex-col gap-3 py-3 2xl:max-w-[100rem] lg:flex-row lg:items-center lg:justify-between lg:py-4">
        {/* Identité + statut */}
        <div className="flex min-w-0 flex-col gap-2 lg:flex-row lg:items-center lg:gap-3">
          <div className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2">
              <div className="grid size-9 shrink-0 place-items-center rounded-md border border-line-strong bg-surface-2">
                <Bot className="size-5 text-slate-300" aria-hidden />
              </div>
              <div className="min-w-0">
                <h1 className="text-base leading-tight font-bold text-white">MT5 Trading Bot</h1>
                <p className="truncate text-xs text-slate-400">
                  {status ? `${status.symbols.join(" · ")} · ${status.timeframe}` : "Dashboard d'administration"}
                </p>
              </div>
            </div>
            <div className="flex shrink-0 gap-2 lg:hidden">
              <UtilityButtons canInstall={canInstall} onInstall={onInstall} onOpenSettings={onOpenSettings} />
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <StatePill status={apiConnected ? status : null} />
            {accountLabel && (
              <span className="inline-flex items-center rounded border border-sky-500/40 bg-sky-500/10 px-2.5 py-1 text-xs font-medium text-sky-200">
                Compte : {accountLabel}
              </span>
            )}
            <ModePill mode={apiConnected ? status?.mode : undefined} />
            {status && (
              <span className="hidden items-center gap-1.5 text-xs text-slate-400 sm:inline-flex">
                <Activity className="size-3.5" aria-hidden />
                Tour #{status.cycle_count} {timeAgo(status.last_cycle_at)} · maj {formatTime(lastUpdated)}
              </span>
            )}
          </div>
        </div>

        {/* Commandes */}
        <div className="grid grid-cols-3 gap-2 lg:flex lg:items-center">
          {canStart ? (
            <Button
              variant="success"
              size="lg"
              className="px-2 lg:px-5"
              icon={<Play className="size-4" />}
              loading={busy === "start"}
              disabled={controlsDisabled}
              onClick={() => run("start", onStart)}
            >
              Démarrer
            </Button>
          ) : (
            <Button
              variant="warning"
              size="lg"
              className="px-2 lg:px-5"
              icon={<Pause className="size-4" />}
              loading={busy === "stop"}
              disabled={controlsDisabled}
              onClick={() => run("stop", onStop)}
            >
              Pause
            </Button>
          )}

          <div
            className="flex h-12 gap-1 rounded-md border border-line bg-surface-2 p-0.5"
            role="group"
            aria-label="Mode d'exécution"
          >
            <button
              type="button"
              disabled={controlsDisabled || !isLive}
              aria-pressed={!isLive}
              onClick={() => run("mode", () => onSetMode(false))}
              className={`${segment} ${!isLive ? "bg-emerald-500/80 text-white" : "text-slate-300 hover:bg-line"}`}
            >
              PAPER
            </button>
            <button
              type="button"
              disabled={controlsDisabled || isLive}
              aria-pressed={isLive}
              onClick={() => setLiveDialogOpen(true)}
              className={`${segment} ${isLive ? "bg-rose-600 text-white" : "text-slate-300 hover:bg-line"}`}
            >
              LIVE
            </button>
          </div>

          <Button
            variant="danger"
            size="lg"
            icon={<Power className="size-4" />}
            disabled={!apiConnected || busy !== null}
            loading={busy === "kill"}
            onClick={() => setKillDialogOpen(true)}
            className="px-2 tracking-wider uppercase lg:px-5"
          >
            <span className="sm:hidden">Kill</span>
            <span className="hidden sm:inline">Kill switch</span>
          </Button>

          <div className="hidden gap-2 lg:flex">
            <UtilityButtons canInstall={canInstall} onInstall={onInstall} onOpenSettings={onOpenSettings} />
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={liveDialogOpen}
        title="Passer en mode LIVE"
        tone="danger"
        confirmLabel="Activer le LIVE"
        requireText="LIVE"
        busy={busy === "mode"}
        description={
          <>
            <p>
              Les prochains ordres du bot seront <strong className="text-white">envoyés au broker</strong> et
              engageront de l'<strong className="text-rose-300">argent réel</strong>{onAccount}.
            </p>
            <p>Vérifiez la configuration du risque avant de continuer.</p>
          </>
        }
        onCancel={() => setLiveDialogOpen(false)}
        onConfirm={() =>
          run("mode", async () => {
            await onSetMode(true);
            setLiveDialogOpen(false);
          })
        }
      />

      <ConfirmDialog
        open={killDialogOpen}
        title="Kill switch"
        tone="danger"
        confirmLabel="Tout fermer maintenant"
        busy={busy === "kill"}
        description={
          <p>
            Le moteur est mis en pause puis les positions sont <strong className="text-white">fermées au prix du
            marché</strong>{onAccount}. La clôture est réelle, même en mode PAPER.
          </p>
        }
        onCancel={() => setKillDialogOpen(false)}
        onConfirm={() =>
          run("kill", async () => {
            await onKillSwitch(includeManual);
            setKillDialogOpen(false);
          })
        }
      >
        <label className="flex min-h-11 items-center gap-3 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={includeManual}
            onChange={(e) => setIncludeManual(e.target.checked)}
            className="size-5 accent-rose-500"
          />
          Fermer aussi les positions ouvertes manuellement
        </label>
      </ConfirmDialog>
    </header>
  );
}
