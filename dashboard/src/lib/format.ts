const LOCALE = "fr-FR";

/**
 * Montant au centime près : toujours 2 décimales (100 000,50 USD, 0,07 USD), quelle que soit
 * la devise. Code ISO (« USD ») plutôt que symbole localisé (« $US ») : convention des terminaux.
 */
export function formatMoney(value: number, currency: string): string {
  const cents = { minimumFractionDigits: 2, maximumFractionDigits: 2 };
  try {
    return new Intl.NumberFormat(LOCALE, { style: "currency", currency, currencyDisplay: "code", ...cents }).format(value);
  } catch {
    return `${new Intl.NumberFormat(LOCALE, cents).format(value)} ${currency}`; // code devise non reconnu par Intl
  }
}

/** Montant signé au centime (+0,07 USD / −1,25 USD), pour les P/L. */
export function formatSignedMoney(value: number, currency: string): string {
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${formatMoney(Math.abs(value), currency)}`;
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

/** Heure locale « HH:MM » (ou « HH:MM:SS » avec `seconds`). */
export function formatClock(iso: string | null, seconds = false): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) });
}

/** Date courte locale « 28 oct. » si différente d'aujourd'hui, sinon chaîne vide. */
export function formatDayIfNotToday(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime()) || date.toDateString() === new Date().toDateString()) return "";
  return date.toLocaleDateString(LOCALE, { day: "numeric", month: "short" });
}

/** Prix avec un nombre fixe de décimales (1,13220 et non 1,1322) : pas de saut de largeur à chaque tick. */
export function formatPrice(value: number, digits: number): string {
  return new Intl.NumberFormat(LOCALE, { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
}
