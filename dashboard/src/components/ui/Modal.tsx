import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";

interface ModalProps {
  open: boolean;
  labelledBy: string;
  /** Absent : la fenêtre ne peut pas être fermée par Échap ou clic extérieur */
  onClose?: () => void;
  children: ReactNode;
}

/**
 * Fenêtre modale : feuille ancrée en bas sur mobile (au-dessus de la barre gestuelle),
 * boîte centrée à partir de `sm`. Échap et clic sur le fond la ferment si `onClose` est fourni.
 * Rendue dans <body> (portail) : un parent avec `backdrop-filter` (en-tête) capturerait sinon
 * le positionnement `fixed` et la fenêtre ne couvrirait plus l'écran.
 */
export function Modal({ open, labelledBy, onClose, children }: ModalProps) {
  useEffect(() => {
    if (!open || !onClose) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/70 sm:items-center sm:p-4"
      onMouseDown={(e) => e.target === e.currentTarget && onClose?.()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        className="pb-sheet max-h-[92dvh] w-full overflow-y-auto overscroll-contain rounded-t-lg border border-line bg-surface px-5 pt-6 sm:max-w-md sm:rounded-md sm:p-6"
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}
