import { useState, useEffect, useCallback } from 'react';

interface UseApiState<T> {
  data: T | null;
  loading: boolean;
  error: Error | null;
}

interface UseApiResult<T> extends UseApiState<T> {
  refetch: () => Promise<void>;
}

/**
 * Hook for API calls with loading and error states
 */
export function useApi<T>(
  apiCall: () => Promise<T>,
  dependencies: unknown[] = []
): UseApiResult<T> {
  const [state, setState] = useState<UseApiState<T>>({
    data: null,
    loading: true,
    error: null,
  });

  const fetchData = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const data = await apiCall();
      setState({ data, loading: false, error: null });
    } catch (error) {
      setState({ data: null, loading: false, error: error as Error });
    }
  }, [apiCall]);

  useEffect(() => {
    fetchData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, dependencies);

  return {
    ...state,
    refetch: fetchData,
  };
}

/**
 * Hook for lazy API calls (triggered manually)
 */
export function useLazyApi<T, Args extends unknown[]>(
  apiCall: (...args: Args) => Promise<T>
): [
  (...args: Args) => Promise<T | null>,
  UseApiState<T>
] {
  const [state, setState] = useState<UseApiState<T>>({
    data: null,
    loading: false,
    error: null,
  });

  const execute = useCallback(async (...args: Args) => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const data = await apiCall(...args);
      setState({ data, loading: false, error: null });
      return data;
    } catch (error) {
      setState({ data: null, loading: false, error: error as Error });
      return null;
    }
  }, [apiCall]);

  return [execute, state];
}

/**
 * Hook for paginated API calls
 */
export function usePaginatedApi<T>(
  apiCall: (page: number, limit: number) => Promise<{ data: T[]; total: number }>,
  initialLimit = 20
): {
  data: T[];
  total: number;
  page: number;
  limit: number;
  loading: boolean;
  error: Error | null;
  setPage: (page: number) => void;
  setLimit: (limit: number) => void;
  refetch: () => void;
} {
  const [page, setPage] = useState(1);
  const [limit, setLimit] = useState(initialLimit);
  const [state, setState] = useState<{
    data: T[];
    total: number;
    loading: boolean;
    error: Error | null;
  }>({
    data: [],
    total: 0,
    loading: true,
    error: null,
  });

  const fetchData = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const result = await apiCall(page, limit);
      setState({
        data: result.data,
        total: result.total,
        loading: false,
        error: null,
      });
    } catch (error) {
      setState((prev) => ({
        ...prev,
        loading: false,
        error: error as Error,
      }));
    }
  }, [apiCall, page, limit]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  return {
    ...state,
    page,
    limit,
    setPage,
    setLimit,
    refetch: fetchData,
  };
}
