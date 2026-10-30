import type { ReactNode } from "react";

interface GlassCardProps {
  title?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  className?: string;
  children: ReactNode;
}

/**
 * Panneau plat à bordure fine, avec bandeau de titre optionnel (style terminal).
 * Bandeau de hauteur fixe (h-12) : les en-têtes de cartes voisines restent alignés quel que
 * soit leur contenu. Marges intérieures : 16 px sur mobile, 20 px à partir de `sm`.
 */
export function GlassCard({ title, icon, actions, className = "", children }: GlassCardProps) {
  return (
    <section
      className={`min-w-0 overflow-hidden rounded-md border border-line bg-surface ${className}`}
    >
      {(title || actions) && (
        <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-line bg-surface-2 px-4 sm:px-5">
          <h2 className="flex min-w-0 items-center gap-2 text-xs font-semibold tracking-[0.08em] text-slate-200 uppercase">
            {icon && <span className="shrink-0 text-slate-500">{icon}</span>}
            <span className="truncate">{title}</span>
          </h2>
          {actions && <div className="flex min-w-0 shrink-0 items-center justify-end">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-md bg-line ${className}`} />;
}
