import { CandlestickChart, DatabaseZap, ListChecks, Newspaper, Wallet } from "lucide-react";

export type Tab = "account" | "positions" | "news" | "engine" | "data";

const TABS: { id: Tab; label: string; Icon: typeof Wallet }[] = [
  { id: "account", label: "Compte", Icon: Wallet },
  { id: "positions", label: "Positions", Icon: CandlestickChart },
  { id: "news", label: "Actualités", Icon: Newspaper },
  { id: "engine", label: "Moteur", Icon: ListChecks },
  { id: "data", label: "Données", Icon: DatabaseZap },
];

interface MobileNavProps {
  active: Tab;
  positionCount: number;
  onChange: (tab: Tab) => void;
}

/** Barre de navigation inférieure (mobile / tablette), au-dessus de la barre gestuelle. */
export function MobileNav({ active, positionCount, onChange }: MobileNavProps) {
  return (
    <nav
      className="pb-safe fixed inset-x-0 bottom-0 z-40 border-t border-line bg-canvas/95 lg:hidden"
      aria-label="Navigation principale"
    >
      <ul className="mx-auto grid max-w-lg grid-cols-5">
        {TABS.map(({ id, label, Icon }) => {
          const selected = id === active;
          return (
            <li key={id}>
              <button
                type="button"
                onClick={() => onChange(id)}
                aria-current={selected ? "page" : undefined}
                className={`relative flex min-h-14 w-full flex-col items-center justify-center gap-1 text-[10px] font-medium transition sm:text-[11px] ${
                  selected ? "text-sky-300" : "text-slate-400 active:text-slate-200"
                }`}
              >
                {selected && <span className="absolute top-0 h-0.5 w-10 rounded-full bg-sky-400" aria-hidden />}
                <span className="relative">
                  <Icon className="size-5" aria-hidden />
                  {id === "positions" && positionCount > 0 && (
                    <span className="absolute -top-1.5 -right-2.5 grid min-w-4 place-items-center rounded-full bg-sky-500 px-1 text-[10px] leading-4 font-bold text-white">
                      {positionCount}
                    </span>
                  )}
                </span>
                {label}
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
