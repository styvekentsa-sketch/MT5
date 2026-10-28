import { CircleCheck, CircleAlert, Download, Eye, EyeOff, Server, Share, SquarePlus } from "lucide-react";
import { useEffect, useId, useState, type FormEvent } from "react";
import {
  api,
  DEFAULT_API_URL,
  errorMessage,
  isMixedContent,
  normalizeBaseUrl,
  type Connection,
} from "../api/client";
import { Button } from "./ui/Button";
import { Modal } from "./ui/Modal";

interface InstallState {
  canInstall: boolean;
  installed: boolean;
  ios: boolean;
  install: () => Promise<boolean>;
}

interface SettingsDialogProps {
  open: boolean;
  connection: Connection;
  /** Erreur à afficher à l'ouverture (ex : clé refusée par le serveur) */
  error: string | null;
  /** False tant qu'aucune connexion valide n'est configurée : la fenêtre ne peut pas être fermée */
  dismissable: boolean;
  installState: InstallState;
  onSave: (connection: Connection) => void;
  onForgetKey: () => void;
  onClose: () => void;
}

type TestResult = { ok: boolean; message: string } | null;

const inputClass =
  "h-12 w-full rounded-md border border-line bg-canvas px-3 font-mono text-base text-white outline-none focus:border-sky-400 sm:text-sm";

/** Paramètres de connexion (URL de l'API, clé) et installation de l'application. */
export function SettingsDialog({
  open,
  connection,
  error,
  dismissable,
  installState,
  onSave,
  onForgetKey,
  onClose,
}: SettingsDialogProps) {
  const titleId = useId();
  const [baseUrl, setBaseUrl] = useState(connection.baseUrl);
  const [apiKey, setApiKey] = useState(connection.apiKey);
  const [visible, setVisible] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestResult>(null);

  useEffect(() => {
    if (!open) return;
    setBaseUrl(connection.baseUrl);
    setApiKey(connection.apiKey);
    setTestResult(null);
  }, [open, connection]);

  const normalizedUrl = normalizeBaseUrl(baseUrl);
  const urlInvalid = baseUrl.trim() !== "" && !normalizedUrl;
  const canSubmit = normalizedUrl !== "" && apiKey.trim() !== "";
  const draft: Connection = { baseUrl: normalizedUrl, apiKey: apiKey.trim() };

  const testConnection = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const status = await api.status(draft);
      setTestResult({ ok: true, message: `Connecté : moteur ${status.state}, mode ${status.mode}` });
    } catch (err) {
      setTestResult({ ok: false, message: errorMessage(err) });
    } finally {
      setTesting(false);
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (canSubmit) onSave(draft);
  };

  return (
    <Modal open={open} labelledBy={titleId} onClose={dismissable ? onClose : undefined}>
      <form onSubmit={submit} className="space-y-5">
        <div>
          <h2 id={titleId} className="flex items-center gap-2 text-lg font-semibold text-white">
            <Server className="size-5 text-sky-400" aria-hidden />
            Connexion au bot
          </h2>
          <p className="mt-1 text-sm text-slate-400">Paramètres mémorisés sur cet appareil.</p>
        </div>

        {error && (
          <p className="rounded-md border border-rose-500/40 bg-rose-500/10 p-3 text-sm text-rose-200">{error}</p>
        )}

        <label className="block space-y-1.5 text-sm text-slate-300">
          <span>URL de l'API</span>
          <input
            type="url"
            inputMode="url"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder={DEFAULT_API_URL}
            autoCapitalize="none"
            autoComplete="off"
            spellCheck={false}
            className={inputClass}
          />
          <span className="block text-xs text-slate-500">
            Même PC : <span className="font-mono">http://127.0.0.1:8000</span> · réseau local :{" "}
            <span className="font-mono">http://192.168.x.x:8000</span> · VPS :{" "}
            <span className="font-mono">https://bot.mondomaine.fr</span>
          </span>
          {urlInvalid && <span className="block text-xs text-rose-300">URL invalide (http:// ou https:// attendu)</span>}
          {normalizedUrl && isMixedContent(normalizedUrl) && (
            <span className="block text-xs text-amber-300">
              Cette page est en HTTPS : le navigateur bloquera une API en HTTP. Exposez l'API en HTTPS.
            </span>
          )}
        </label>

        <label className="block space-y-1.5 text-sm text-slate-300">
          <span>
            Clé API <span className="text-slate-500">(API_KEY du .env du bot)</span>
          </span>
          <div className="relative">
            <input
              type={visible ? "text" : "password"}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              autoCapitalize="none"
              autoComplete="off"
              spellCheck={false}
              className={`${inputClass} pr-12`}
            />
            <button
              type="button"
              onClick={() => setVisible((v) => !v)}
              className="absolute top-1/2 right-0.5 grid size-11 -translate-y-1/2 place-items-center rounded-md text-slate-400 hover:text-white"
              aria-label={visible ? "Masquer la clé" : "Afficher la clé"}
            >
              {visible ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
            </button>
          </div>
        </label>

        {testResult && (
          <p
            className={`flex items-start gap-2 rounded-md border p-3 text-sm ${
              testResult.ok
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
                : "border-rose-500/40 bg-rose-500/10 text-rose-200"
            }`}
          >
            {testResult.ok ? <CircleCheck className="mt-0.5 size-4 shrink-0" /> : <CircleAlert className="mt-0.5 size-4 shrink-0" />}
            {testResult.message}
          </p>
        )}

        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          {dismissable && (
            <Button size="lg" onClick={onClose}>
              Annuler
            </Button>
          )}
          <Button size="lg" onClick={testConnection} loading={testing} disabled={!canSubmit}>
            Tester
          </Button>
          <Button size="lg" type="submit" variant="primary" disabled={!canSubmit}>
            Enregistrer
          </Button>
        </div>

        {connection.apiKey && (
          <button type="button" onClick={onForgetKey} className="min-h-11 text-sm text-rose-300 hover:text-rose-200">
            Oublier la clé sur cet appareil
          </button>
        )}

        <InstallSection state={installState} />
      </form>
    </Modal>
  );
}

function InstallSection({ state }: { state: InstallState }) {
  if (state.installed) {
    return (
      <p className="flex items-center gap-2 border-t border-line pt-4 text-sm text-emerald-300">
        <CircleCheck className="size-4" aria-hidden /> Application installée sur cet appareil
      </p>
    );
  }
  if (state.canInstall) {
    return (
      <div className="border-t border-line pt-4">
        <Button size="lg" variant="primary" className="w-full" icon={<Download className="size-4" />} onClick={() => void state.install()}>
          Installer l'application
        </Button>
      </div>
    );
  }
  if (state.ios) {
    return (
      <div className="space-y-1 border-t border-line pt-4 text-sm text-slate-300">
        <p className="font-semibold text-white">Installer sur iPhone / iPad</p>
        <p className="flex flex-wrap items-center gap-1">
          Dans Safari, touchez <Share className="size-4 text-sky-400" aria-label="Partager" /> puis
          <SquarePlus className="size-4 text-sky-400" aria-hidden /> « Sur l'écran d'accueil ».
        </p>
      </div>
    );
  }
  return null;
}
