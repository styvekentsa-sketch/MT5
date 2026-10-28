import { Activity, Bot, KeyRound, Pause, Play, Power } from "lucide-react";
import { useState } from "react";
import type { Mode, Status } from "../api/types";
import { formatTime, timeAgo } from "../lib/format";
import { Button } from "./ui/Button";
import { ConfirmDialog } from "./ui/ConfirmDialog";

interface ControlPanelProps {
  status: Status | null;
  apiConnected: boolean;
  lastUpdated: Date | null;
  onStart: () => Promise<void>;
  onStop: () => Promise<void>;
  onSetMode: (live: boolean) => Promise<void>;
  onKillSwitch: (includeManual: boolean) => Promise<void>;
  onOpenSettings: () => void;
}

type BusyAction = "start" | "stop" | "mode" | "kill";

const STATE_STYLES = {
  running: { label: "En marche", dot: "bg-emerald-400 animate-pulse", text: "text-emerald-300" },
  paused: { label: "En pause", dot: "bg-amber-400", text: "text-amber-300" },
  stopped: { label: "Arrêté", dot: "bg-slate-500", text: "text-slate-400" },
} as const;

function StatePill({ status }: { status: Status | null }) {
  const style = status ? STATE_STYLES[status.state] : null;
  return (
    <span className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs font-semibold">
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
      className={`inline-flex items-center rounded-full px-3 py-1 text-xs font-bold tracking-wider ${
        live
          ? "bg-rose-500/20 text-rose-300 ring-1 ring-rose-500/50"
          : "bg-emerald-500/15 text-emerald-300 ring-1 ring-emerald-500/40"
      }`}
    >
      {live ? "LIVE · argent réel" : "PAPER · simulation"}
    </span>
  );
}

/** En-tête : statut du bot et commandes globales (start/pause, mode, kill-switch). */
export function ControlPanel({
  status,
  apiConnected,
  lastUpdated,
  onStart,
  onStop,
  onSetMode,
  onKillSwitch,
  onOpenSettings,
}: ControlPanelProps) {
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
  const isPaused = status?.state === "paused";

  return (
    <header className="sticky top-0 z-40 border-b border-white/10 bg-slate-950/60 backdrop-blur-xl">
      <div className="mx-auto flex max-w-7xl flex-col gap-4 px-4 py-4 lg:flex-row lg:items-center lg:justify-between">
        {/* Identité + statut */}
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <div className="grid size-9 place-items-center rounded-xl bg-gradient-to-br from-sky-500 to-indigo-600 shadow-lg shadow-sky-900/40">
              <Bot className="size-5 text-white" aria-hidden />
            </div>
            <div>
              <h1 className="text-base leading-tight font-bold text-white">MT5 Trading Bot</h1>
              <p className="text-xs text-slate-400">
                {status ? `${status.symbols.join(" · ")} · ${status.timeframe}` : "Dashboard d'administration"}
              </p>
            </div>
          </div>
          <StatePill status={apiConnected ? status : null} />
          <ModePill mode={apiConnected ? status?.mode : undefined} />
          {status && (
            <span className="hidden items-center gap-1.5 text-xs text-slate-400 sm:inline-flex">
              <Activity className="size-3.5" aria-hidden />
              Tour #{status.cycle_count} {timeAgo(status.last_cycle_at)} · maj {formatTime(lastUpdated)}
            </span>
          )}
        </div>

        {/* Commandes */}
        <div className="flex flex-wrap items-center gap-2">
          {isPaused || status?.state === "stopped" ? (
            <Button
              variant="success"
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
              icon={<Pause className="size-4" />}
              loading={busy === "stop"}
              disabled={controlsDisabled}
              onClick={() => run("stop", onStop)}
            >
              Pause
            </Button>
          )}

          <div className="inline-flex rounded-xl border border-white/10 bg-white/5 p-1" role="group" aria-label="Mode d'exécution">
            <button
              type="button"
              disabled={controlsDisabled || !isLive}
              onClick={() => run("mode", () => onSetMode(false))}
              className={`rounded-lg px-3 py-1.5 text-xs font-bold transition disabled:cursor-not-allowed ${
                !isLive ? "bg-emerald-500/80 text-white" : "text-slate-300 hover:bg-white/10"
              }`}
            >
              PAPER
            </button>
            <button
              type="button"
              disabled={controlsDisabled || isLive}
              onClick={() => setLiveDialogOpen(true)}
              className={`rounded-lg px-3 py-1.5 text-xs font-bold transition disabled:cursor-not-allowed ${
                isLive ? "bg-rose-600 text-white" : "text-slate-300 hover:bg-white/10"
              }`}
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
            className="uppercase tracking-wider"
          >
            Kill switch
          </Button>

          <Button size="md" onClick={onOpenSettings} aria-label="Clé API" title="Clé API" icon={<KeyRound className="size-4" />} />
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
              engageront de l'<strong className="text-rose-300">argent réel</strong>.
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
            marché</strong>. La clôture est réelle, même en mode PAPER.
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
        <label className="flex items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={includeManual}
            onChange={(e) => setIncludeManual(e.target.checked)}
            className="size-4 accent-rose-500"
          />
          Fermer aussi les positions ouvertes manuellement
        </label>
      </ConfirmDialog>
    </header>
  );
}
