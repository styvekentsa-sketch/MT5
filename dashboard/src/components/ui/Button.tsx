import { LoaderCircle } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "primary" | "ghost" | "success" | "warning" | "danger";
type Size = "sm" | "md" | "lg";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-sky-500 text-white hover:bg-sky-400 focus-visible:ring-sky-400",
  ghost: "border border-line bg-surface-2 text-slate-200 hover:bg-line focus-visible:ring-white/40",
  success: "bg-emerald-600 text-white hover:bg-emerald-500 focus-visible:ring-emerald-400",
  warning: "border border-amber-500/60 bg-amber-500/15 text-amber-200 hover:bg-amber-500/25 focus-visible:ring-amber-400",
  danger: "bg-rose-600 text-white hover:bg-rose-500 focus-visible:ring-rose-400",
};

// md = 44 px (minimum tactile recommandé), lg = 48 px (actions critiques : kill switch, start/stop)
const SIZES: Record<Size, string> = {
  sm: "h-9 gap-1.5 px-3 text-xs",
  md: "h-11 gap-2 px-4 text-sm",
  lg: "h-12 gap-2 px-5 text-sm",
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  icon?: ReactNode;
}

export function Button({
  variant = "ghost",
  size = "md",
  loading = false,
  icon,
  disabled,
  className = "",
  children,
  ...props
}: ButtonProps) {
  return (
    <button
      type="button"
      disabled={disabled || loading}
      className={`inline-flex items-center justify-center rounded-md font-sans font-medium whitespace-nowrap transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950 disabled:cursor-not-allowed disabled:opacity-50 ${VARIANTS[variant]} ${SIZES[size]} ${className}`}
      {...props}
    >
      {loading ? <LoaderCircle className="size-4 animate-spin" aria-hidden /> : icon}
      {children}
    </button>
  );
}
