import { Eye, EyeOff, KeyRound } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { API_URL } from "../api/client";
import { Button } from "./ui/Button";

interface ApiKeyDialogProps {
  open: boolean;
  hasKey: boolean;
  error: string | null;
  onSave: (key: string) => void;
  onClear: () => void;
  onCancel: () => void;
}

/** Saisie de la clé API, conservée dans le localStorage du navigateur. */
export function ApiKeyDialog({ open, hasKey, error, onSave, onClear, onCancel }: ApiKeyDialogProps) {
  const [value, setValue] = useState("");
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (open) setValue("");
  }, [open]);

  if (!open) return null;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (value.trim()) onSave(value);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-md">
      <form
        onSubmit={submit}
        role="dialog"
        aria-modal="true"
        aria-labelledby="api-key-title"
        className="w-full max-w-md rounded-2xl border border-white/10 bg-slate-900/90 p-6 shadow-2xl backdrop-blur-xl"
      >
        <h2 id="api-key-title" className="flex items-center gap-2 text-lg font-semibold text-white">
          <KeyRound className="size-5 text-sky-400" aria-hidden />
          Connexion à l'API du bot
        </h2>
        <p className="mt-2 text-sm text-slate-400">
          Saisissez la valeur de <span className="font-mono text-slate-200">API_KEY</span> définie dans le fichier
          <span className="font-mono text-slate-200"> .env</span> du bot. Elle est envoyée dans l'en-tête
          <span className="font-mono text-slate-200"> X-API-Key</span> vers{" "}
          <span className="font-mono text-slate-200">{API_URL}</span>.
        </p>

        {error && (
          <p className="mt-4 rounded-xl border border-rose-500/40 bg-rose-500/10 p-3 text-sm text-rose-200">{error}</p>
        )}

        <div className="relative mt-4">
          <input
            autoFocus
            type={visible ? "text" : "password"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="Clé API"
            autoComplete="off"
            spellCheck={false}
            className="w-full rounded-xl border border-white/10 bg-slate-950/60 px-3 py-2.5 pr-10 font-mono text-sm text-white outline-none focus:border-sky-400"
          />
          <button
            type="button"
            onClick={() => setVisible((v) => !v)}
            className="absolute top-1/2 right-2 -translate-y-1/2 rounded-md p-1 text-slate-400 hover:text-white"
            aria-label={visible ? "Masquer la clé" : "Afficher la clé"}
          >
            {visible ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
          </button>
        </div>
        <p className="mt-2 text-xs text-slate-500">La clé est mémorisée dans ce navigateur (localStorage).</p>

        <div className="mt-6 flex flex-wrap justify-between gap-2">
          {hasKey ? (
            <Button onClick={onClear} className="text-rose-300">
              Oublier la clé
            </Button>
          ) : (
            <span />
          )}
          <div className="flex gap-2">
            {hasKey && !error && <Button onClick={onCancel}>Annuler</Button>}
            <Button type="submit" variant="primary" disabled={!value.trim()}>
              Se connecter
            </Button>
          </div>
        </div>
      </form>
    </div>
  );
}
