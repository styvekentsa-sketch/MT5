import type { ReactNode } from "react";

interface GlassCardProps {
  title?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  className?: string;
  children: ReactNode;
}

/** Conteneur translucide (glassmorphism) avec en-tête optionnel. */
export function GlassCard({ title, icon, actions, className = "", children }: GlassCardProps) {
  return (
    <section
      className={`rounded-2xl border border-white/10 bg-white/[0.04] shadow-xl shadow-black/20 backdrop-blur-xl ${className}`}
    >
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-white/5 px-5 py-3.5">
          <h2 className="flex items-center gap-2 text-sm font-semibold tracking-wide text-slate-200">
            {icon && <span className="text-sky-400">{icon}</span>}
            {title}
          </h2>
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-md bg-white/10 ${className}`} />;
}
