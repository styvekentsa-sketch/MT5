import type { ReactNode } from "react";

interface GlassCardProps {
  title?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  className?: string;
  children: ReactNode;
}

/** Panneau plat à bordure fine, avec bandeau de titre optionnel (style terminal). */
export function GlassCard({ title, icon, actions, className = "", children }: GlassCardProps) {
  return (
    <section
      className={`rounded-md border border-line bg-surface ${className}`}
    >
      {(title || actions) && (
        <header className="flex min-h-11 items-center justify-between gap-3 border-b border-line bg-surface-2 px-5 py-2.5">
          <h2 className="flex items-center gap-2 text-xs font-semibold tracking-[0.08em] text-slate-200 uppercase">
            {icon && <span className="text-slate-500">{icon}</span>}
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
  return <div className={`animate-pulse rounded-md bg-line ${className}`} />;
}
