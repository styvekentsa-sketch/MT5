import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";

export interface PollingState<T> {
  data: T | null;
  error: ApiError | null;
  lastUpdated: Date | null;
  refresh: () => void;
}

/**
 * Interroge `fetcher` toutes les `intervalMs` millisecondes, en économisant la batterie :
 * - aucune requête quand l'onglet / l'app est en arrière-plan ou que l'appareil est hors ligne ;
 *   un appel part immédiatement au retour au premier plan ou du réseau
 * - la requête suivante ne part qu'une fois la précédente terminée (pas de chevauchement)
 * - en cas d'erreur, la dernière donnée valide est conservée
 * - `refresh()` relance un appel immédiatement (ex : après une action)
 */
export function usePolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  intervalMs: number,
  enabled = true,
): PollingState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [trigger, setTrigger] = useState(0);
  const fetcherRef = useRef(fetcher);

  useEffect(() => {
    fetcherRef.current = fetcher;
  }, [fetcher]);

  const refresh = useCallback(() => setTrigger((n) => n + 1), []);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    let timer: number | undefined;
    let inFlight = false;

    const canPoll = () => !document.hidden && navigator.onLine;

    const run = async () => {
      if (inFlight || controller.signal.aborted) return;
      if (!canPoll()) return; // reprise via les événements visibilitychange / online
      inFlight = true;
      try {
        const result = await fetcherRef.current(controller.signal);
        if (controller.signal.aborted) return;
        setData(result);
        setError(null);
        setLastUpdated(new Date());
      } catch (err) {
        if (controller.signal.aborted) return;
        setError(err instanceof ApiError ? err : new ApiError(String(err), "http"));
      } finally {
        inFlight = false;
      }
      window.clearTimeout(timer);
      timer = window.setTimeout(run, intervalMs);
    };

    const wake = () => {
      if (!canPoll()) return;
      window.clearTimeout(timer);
      void run();
    };

    document.addEventListener("visibilitychange", wake);
    window.addEventListener("online", wake);
    void run();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", wake);
      window.removeEventListener("online", wake);
    };
  }, [enabled, intervalMs, trigger]);

  return { data, error, lastUpdated, refresh };
}
