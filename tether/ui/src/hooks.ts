import { useCallback, useEffect, useState } from "react";

export interface Async<T> {
  data: T | undefined;
  error: string | undefined;
  loading: boolean;
  reload: () => void;
}

/** Run an async loader whenever `deps` change; expose data/error/loading + reload. */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[]): Async<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const load = useCallback(loader, deps);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    load()
      .then((d) => {
        if (alive) {
          setData(d);
          setError(undefined);
        }
      })
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [load, tick]);
  return { data, error, loading, reload: () => setTick((t) => t + 1) };
}
