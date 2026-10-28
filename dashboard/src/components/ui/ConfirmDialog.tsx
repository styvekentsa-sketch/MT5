import { TriangleAlert, X } from "lucide-react";
import { useEffect, useId, useState, type ReactNode } from "react";
import { Button } from "./Button";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  tone?: "danger" | "warning" | "primary";
  /** Texte à recopier pour activer le bouton (ex : "LIVE") */
  requireText?: string;
  busy?: boolean;
  children?: ReactNode;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Boîte de confirmation modale ; Échap ou clic hors du panneau pour annuler. */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  tone = "danger",
  requireText,
  busy = false,
  children,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const [typed, setTyped] = useState("");
  const titleId = useId();

  useEffect(() => {
    if (!open) {
      setTyped("");
      return;
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, onCancel]);

  if (!open) return null;
  const canConfirm = !requireText || typed.trim() === requireText;
  const accent = tone === "danger" ? "text-rose-400" : tone === "warning" ? "text-amber-400" : "text-sky-400";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 p-4 backdrop-blur-sm"
      onMouseDown={(e) => e.target === e.currentTarget && !busy && onCancel()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="w-full max-w-md rounded-2xl border border-white/10 bg-slate-900/90 p-6 shadow-2xl backdrop-blur-xl"
      >
        <div className="mb-4 flex items-start justify-between gap-4">
          <h3 id={titleId} className={`flex items-center gap-2 text-lg font-semibold ${accent}`}>
            <TriangleAlert className="size-5" aria-hidden />
            {title}
          </h3>
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="rounded-lg p-1 text-slate-400 hover:bg-white/10 hover:text-white"
            aria-label="Fermer"
          >
            <X className="size-4" />
          </button>
        </div>

        <div className="space-y-3 text-sm leading-relaxed text-slate-300">{description}</div>
        {children && <div className="mt-4">{children}</div>}

        {requireText && (
          <label className="mt-4 block text-sm text-slate-300">
            Tapez <span className="font-mono font-bold text-white">{requireText}</span> pour confirmer
            <input
              autoFocus
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && canConfirm && !busy && onConfirm()}
              className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950/60 px-3 py-2 font-mono text-white outline-none focus:border-rose-400"
            />
          </label>
        )}

        <div className="mt-6 flex justify-end gap-2">
          <Button onClick={onCancel} disabled={busy}>
            Annuler
          </Button>
          <Button variant={tone} onClick={onConfirm} disabled={!canConfirm} loading={busy}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
