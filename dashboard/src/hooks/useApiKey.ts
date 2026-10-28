import { useCallback, useState } from "react";

const STORAGE_KEY = "mt5bot.apiKey";

function readStoredKey(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return ""; // stockage bloqué (navigation privée stricte...)
  }
}

/** Clé API (en-tête X-API-Key), conservée dans le localStorage de ce navigateur. */
export function useApiKey() {
  const [apiKey, setApiKey] = useState(readStoredKey);

  const saveKey = useCallback((key: string) => {
    const trimmed = key.trim();
    try {
      localStorage.setItem(STORAGE_KEY, trimmed);
    } catch {
      // la clé reste valable pour la session en cours
    }
    setApiKey(trimmed);
  }, []);

  const clearKey = useCallback(() => {
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      // rien à nettoyer
    }
    setApiKey("");
  }, []);

  return { apiKey, saveKey, clearKey };
}
