import { useCallback, useState } from "react";
import { DEFAULT_API_URL, normalizeBaseUrl, type Connection } from "../api/client";

const STORAGE_KEY = "mt5bot.connection";
const LEGACY_KEY_STORAGE = "mt5bot.apiKey"; // version précédente : clé seule

function readStored(): Connection {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<Connection>;
      return {
        baseUrl: normalizeBaseUrl(parsed.baseUrl ?? "") || DEFAULT_API_URL,
        apiKey: typeof parsed.apiKey === "string" ? parsed.apiKey : "",
      };
    }
    return { baseUrl: DEFAULT_API_URL, apiKey: localStorage.getItem(LEGACY_KEY_STORAGE) ?? "" };
  } catch {
    return { baseUrl: DEFAULT_API_URL, apiKey: "" }; // stockage bloqué ou corrompu
  }
}

/** URL de l'API et clé API, conservées dans le localStorage de cet appareil. */
export function useConnection() {
  const [connection, setConnection] = useState<Connection>(readStored);

  const save = useCallback((next: Connection) => {
    const clean = { baseUrl: normalizeBaseUrl(next.baseUrl) || DEFAULT_API_URL, apiKey: next.apiKey.trim() };
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(clean));
      localStorage.removeItem(LEGACY_KEY_STORAGE);
    } catch {
      // valable pour la session en cours uniquement
    }
    setConnection(clean);
  }, []);

  const forgetKey = useCallback(() => {
    setConnection((current) => {
      const next = { ...current, apiKey: "" };
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
        localStorage.removeItem(LEGACY_KEY_STORAGE);
      } catch {
        // rien à nettoyer
      }
      return next;
    });
  }, []);

  return { connection, save, forgetKey };
}
