import { CircleAlert, Eye, EyeOff, LogIn } from "lucide-react";
import { useEffect, useId, useState, type FormEvent } from "react";
import type { MT5LoginRequest } from "../api/types";
import { Button } from "./ui/Button";
import { Modal } from "./ui/Modal";

interface MT5LoginDialogProps {
  open: boolean;
  /** Serveur proposé par défaut (celui du compte actif) */
  defaultServer?: string | null;
  onSubmit: (credentials: MT5LoginRequest) => Promise<void>;
  onClose: () => void;
}

const inputClass =
  "h-12 w-full rounded-md border border-line bg-canvas px-3 font-mono text-base text-white outline-none focus:border-sky-400 sm:text-sm";

// Serveurs courants proposés à la saisie (le champ reste libre)
const SERVER_SUGGESTIONS = ["MetaQuotes-Demo", "Exness-MT5Trial", "Exness-MT5Real", "ICMarketsSC-Demo", "ICMarketsSC-MT5"];

/**
 * Connexion à un compte MetaTrader 5 : numéro de compte, mot de passe de trading, serveur.
 * Le mot de passe ne vit que dans l'état de ce formulaire : il est effacé après chaque
 * tentative et à la fermeture, et n'est jamais écrit dans le stockage du navigateur.
 */
export function MT5LoginDialog({ open, defaultServer, onSubmit, onClose }: MT5LoginDialogProps) {
  const titleId = useId();
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [server, setServer] = useState(defaultServer ?? "");
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setPassword("");
      setShowPassword(false);
      setError(null);
    } else if (!server && defaultServer) {
      setServer(defaultServer);
    }
  }, [open, defaultServer, server]);

  const loginNumber = Number(login);
  const valid = /^\d{1,12}$/.test(login) && loginNumber > 0 && password.length > 0 && server.trim().length >= 2;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onSubmit({ login: loginNumber, password, server: server.trim() });
      setLogin("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPassword("");                                   // jamais conservé, même en cas d'échec
      setBusy(false);
    }
  };

  return (
    <Modal open={open} labelledBy={titleId} onClose={busy ? undefined : onClose}>
      <form onSubmit={submit} className="flex flex-col gap-4" autoComplete="on">
        <div>
          <h2 id={titleId} className="flex items-center gap-2 text-lg font-semibold text-white">
            <LogIn className="size-5 text-sky-400" aria-hidden />
            Connexion à un compte MT5
          </h2>
          <p className="mt-1 text-sm text-slate-400">
            Le bot travaillera sur ce compte. Il sera <strong className="text-slate-200">mis en pause et en PAPER</strong> :
            reprenez-le (et passez en LIVE) seulement quand vous êtes prêt.
          </p>
        </div>

        <label className="flex flex-col gap-1.5">
          <span className="text-xs font-medium tracking-[0.06em] text-slate-400 uppercase">Numéro de compte</span>
          <input
            className={inputClass}
            inputMode="numeric"
            autoComplete="username"
            placeholder="5058040678"
            value={login}
            onChange={(e) => setLogin(e.target.value.replace(/\D/g, ""))}
            required
            disabled={busy}
          />
        </label>

        <label className="flex flex-col gap-1.5">
          <span className="text-xs font-medium tracking-[0.06em] text-slate-400 uppercase">Mot de passe de trading</span>
          <div className="relative">
            <input
              className={`${inputClass} pr-12`}
              type={showPassword ? "text" : "password"}
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              disabled={busy}
            />
            <button
              type="button"
              className="absolute inset-y-0 right-0 grid w-12 place-items-center text-slate-400 hover:text-white"
              onClick={() => setShowPassword((v) => !v)}
              aria-label={showPassword ? "Masquer le mot de passe" : "Afficher le mot de passe"}
            >
              {showPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
            </button>
          </div>
        </label>

        <label className="flex flex-col gap-1.5">
          <span className="text-xs font-medium tracking-[0.06em] text-slate-400 uppercase">Serveur du broker</span>
          <input
            className={inputClass}
            list="mt5-servers"
            placeholder="MetaQuotes-Demo"
            value={server}
            onChange={(e) => setServer(e.target.value)}
            required
            disabled={busy}
          />
          <datalist id="mt5-servers">
            {SERVER_SUGGESTIONS.map((s) => (
              <option key={s} value={s} />
            ))}
          </datalist>
          <span className="text-[11px] text-slate-500">Nom exact affiché dans MT5 (Fichier → Se connecter à un compte de trading).</span>
        </label>

        {error && (
          <p role="alert" className="flex items-start gap-2 rounded-md border border-rose-500/40 bg-rose-500/10 p-3 text-sm text-rose-200">
            <CircleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
            {error}
          </p>
        )}

        <p className="text-[11px] text-slate-500">
          Le mot de passe est envoyé une seule fois au bot, gardé uniquement en mémoire du bot pour les reconnexions, et
          jamais enregistré dans ce navigateur. Hors de cet ordinateur, utilisez une connexion HTTPS.
        </p>

        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button type="button" onClick={onClose} disabled={busy}>
            Annuler
          </Button>
          <Button type="submit" variant="primary" loading={busy} disabled={!valid} icon={<LogIn className="size-4" />}>
            {busy ? "Connexion…" : "Se connecter"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
