import { CircleAlert, CircleCheck, Info, X } from "lucide-react";
import type { Toast, ToastTone } from "../../hooks/useToasts";

const STYLES: Record<ToastTone, { className: string; Icon: typeof Info }> = {
  success: { className: "border-emerald-500/40 text-emerald-200", Icon: CircleCheck },
  error: { className: "border-rose-500/40 text-rose-200", Icon: CircleAlert },
  info: { className: "border-sky-500/40 text-sky-200", Icon: Info },
};

export function Toasts({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  return (
    <div className="bottom-mobile-nav pointer-events-none fixed inset-x-4 z-50 flex flex-col gap-2 sm:left-auto sm:w-full sm:max-w-sm lg:bottom-4" aria-live="polite">
      {toasts.map(({ id, tone, message }) => {
        const { className, Icon } = STYLES[tone];
        return (
          <div
            key={id}
            className={`pointer-events-auto flex items-start gap-3 rounded-xl border bg-slate-900/90 p-3 text-sm shadow-xl backdrop-blur-xl ${className}`}
          >
            <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
            <p className="flex-1 break-words">{message}</p>
            <button type="button" onClick={() => onDismiss(id)} className="text-slate-400 hover:text-white" aria-label="Fermer">
              <X className="size-4" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
