import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";

export interface PollingState<T> {
  data: T | null;
  error: ApiError | null;
  lastUpdated: Date | null;
  refresh: () => void;
}

/**
 * Interroge `fetcher` toutes les `intervalMs` millisecondes.
 * - la requête suivante ne part qu'une fois la précédente terminée (pas de chevauchement)
 * - pas d'appel quand l'onglet est masqué
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

    const run = async () => {
      if (!document.hidden) {
        try {
          const result = await fetcherRef.current(controller.signal);
          if (controller.signal.aborted) return;
          setData(result);
          setError(null);
          setLastUpdated(new Date());
        } catch (err) {
          if (controller.signal.aborted) return;
          setError(err instanceof ApiError ? err : new ApiError(String(err), "http"));
        }
      }
      if (!controller.signal.aborted) timer = window.setTimeout(run, intervalMs);
    };

    void run();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [enabled, intervalMs, trigger]);

  return { data, error, lastUpdated, refresh };
}
