const LOCALE = "fr-FR";

export function formatMoney(value: number, currency: string): string {
  try {
    return new Intl.NumberFormat(LOCALE, { style: "currency", currency, maximumFractionDigits: 2 }).format(value);
  } catch {
    return `${formatNumber(value, 2)} ${currency}`; // code devise non reconnu par Intl
  }
}

export function formatNumber(value: number, maxDigits = 5): string {
  return new Intl.NumberFormat(LOCALE, { maximumFractionDigits: maxDigits }).format(value);
}

export function formatSigned(value: number, digits = 2): string {
  const formatted = new Intl.NumberFormat(LOCALE, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Math.abs(value));
  return `${value > 0 ? "+" : value < 0 ? "−" : ""}${formatted}`;
}

export function formatPct(value: number, digits = 2): string {
  return `${formatSigned(value, digits)} %`;
}

const relative = new Intl.RelativeTimeFormat(LOCALE, { numeric: "auto" });

export function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const seconds = Math.round((new Date(iso).getTime() - Date.now()) / 1000);
  if (Number.isNaN(seconds)) return "—";
  const abs = Math.abs(seconds);
  if (abs < 60) return relative.format(seconds, "second");
  if (abs < 3_600) return relative.format(Math.round(seconds / 60), "minute");
  if (abs < 86_400) return relative.format(Math.round(seconds / 3_600), "hour");
  return relative.format(Math.round(seconds / 86_400), "day");
}

export function formatTime(date: Date | null): string {
  return date ? date.toLocaleTimeString(LOCALE) : "—";
}

/** Classe de couleur Tailwind pour un P/L ou un score signé. */
export function signColor(value: number): string {
  if (value > 0) return "text-emerald-400";
  if (value < 0) return "text-rose-400";
  return "text-slate-300";
}

/** N'autorise que les liens http(s) : un flux RSS pourrait contenir un lien `javascript:`. */
export function isSafeUrl(url: string): boolean {
  try {
    const { protocol } = new URL(url);
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}
