import { TriangleAlert, X } from "lucide-react";
import { useEffect, useId, useState, type ReactNode } from "react";
import { Button } from "./Button";
import { Modal } from "./Modal";

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

/** Confirmation d'une action sensible ; Échap ou clic hors du panneau pour annuler. */
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
    if (!open) setTyped("");
  }, [open]);

  const canConfirm = !requireText || typed.trim() === requireText;
  const accent = tone === "danger" ? "text-rose-400" : tone === "warning" ? "text-amber-400" : "text-sky-400";

  return (
    <Modal open={open} labelledBy={titleId} onClose={busy ? undefined : onCancel}>
      <div className="mb-4 flex items-start justify-between gap-4">
        <h3 id={titleId} className={`flex items-center gap-2 text-lg font-semibold ${accent}`}>
          <TriangleAlert className="size-5" aria-hidden />
          {title}
        </h3>
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="-m-2 grid size-11 place-items-center rounded-xl text-slate-400 hover:bg-white/10 hover:text-white"
          aria-label="Fermer"
        >
          <X className="size-5" />
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
            autoCapitalize="characters"
            autoComplete="off"
            spellCheck={false}
            className="mt-2 h-12 w-full rounded-xl border border-white/10 bg-slate-950/60 px-3 font-mono text-base text-white outline-none focus:border-rose-400"
          />
        </label>
      )}

      <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <Button size="lg" onClick={onCancel} disabled={busy}>
          Annuler
        </Button>
        <Button size="lg" variant={tone} onClick={onConfirm} disabled={!canConfirm} loading={busy}>
          {confirmLabel}
        </Button>
      </div>
    </Modal>
  );
}
