import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';

import { DheClient, DheClientOptions } from './client';

const DheClientContext = createContext<DheClient | null>(null);

export interface DheClientProviderProps extends DheClientOptions {
    /** Pass a ready client (e.g. in tests); otherwise one is built from the options. */
    client?: DheClient;
    children?: React.ReactNode;
}

export function DheClientProvider({ client, baseUrl, token, fetchImpl, children }: DheClientProviderProps) {
    const value = useMemo(
        () => client ?? new DheClient({ baseUrl, token, fetchImpl }),
        [client, baseUrl, token, fetchImpl],
    );
    return React.createElement(DheClientContext.Provider, { value }, children);
}

let fallbackClient: DheClient | null = null;

/** Returns the client from context, or a lazily-built default client against `/dhe/api`. */
export function useDheClient(): DheClient {
    const ctx = useContext(DheClientContext);
    if (ctx) return ctx;
    if (!fallbackClient) fallbackClient = new DheClient();
    return fallbackClient;
}

export interface AsyncState<T> {
    data: T | undefined;
    error: Error | undefined;
    loading: boolean;
    /** Re-run the loader. Resolves once the new result is stored. */
    reload: () => Promise<void>;
    /** Replace data locally (optimistic updates). */
    setData: (updater: T | ((prev: T | undefined) => T)) => void;
}

function isAbort(e: unknown): boolean {
    return e instanceof DOMException && e.name === 'AbortError';
}

/**
 * Runs `loader` whenever `deps` change. In-flight requests are aborted on change/unmount, and
 * results of stale requests are ignored.
 */
export function useAsync<T>(
    loader: (signal: AbortSignal) => Promise<T>,
    deps: React.DependencyList,
    options: { enabled?: boolean } = {},
): AsyncState<T> {
    const enabled = options.enabled ?? true;
    const [data, setDataState] = useState<T | undefined>(undefined);
    const [error, setError] = useState<Error | undefined>(undefined);
    const [loading, setLoading] = useState<boolean>(enabled);
    const controllerRef = useRef<AbortController | null>(null);
    const seqRef = useRef(0);
    const loaderRef = useRef(loader);
    loaderRef.current = loader;

    const run = useCallback(async (background = false) => {
        controllerRef.current?.abort();
        const controller = new AbortController();
        controllerRef.current = controller;
        seqRef.current += 1;
        const seq = seqRef.current;
        if (!background) setLoading(true);
        try {
            const result = await loaderRef.current(controller.signal);
            if (seq === seqRef.current) {
                setDataState(result);
                setError(undefined);
            }
        } catch (e) {
            if (!isAbort(e) && seq === seqRef.current) setError(e as Error);
        } finally {
            if (seq === seqRef.current) setLoading(false);
        }
    }, []);

    useEffect(() => {
        if (!enabled) {
            setLoading(false);
            return undefined;
        }
        run();
        return () => controllerRef.current?.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [enabled, run, ...deps]);

    const setData = useCallback((updater: T | ((prev: T | undefined) => T)) => {
        setDataState((prev) =>
            typeof updater === 'function' ? (updater as (p: T | undefined) => T)(prev) : updater,
        );
    }, []);

    const reload = useCallback(() => run(true), [run]);

    return { data, error, loading, reload, setData };
}

/** Like useAsync but also refreshes every `intervalMs` while the tab is visible. */
export function usePolling<T>(
    loader: (signal: AbortSignal) => Promise<T>,
    intervalMs: number,
    deps: React.DependencyList,
    options: { enabled?: boolean } = {},
): AsyncState<T> {
    const state = useAsync(loader, deps, options);
    const { reload } = state;
    const enabled = options.enabled ?? true;

    useEffect(() => {
        if (!enabled || intervalMs <= 0) return undefined;
        const id = setInterval(() => {
            if (typeof document === 'undefined' || document.visibilityState !== 'hidden') {
                reload();
            }
        }, intervalMs);
        return () => clearInterval(id);
    }, [enabled, intervalMs, reload]);

    return state;
}

/** Wraps a mutation with pending/error state. */
export function useMutation<A extends unknown[], R>(fn: (...args: A) => Promise<R>) {
    const [pending, setPending] = useState(false);
    const [error, setError] = useState<Error | undefined>(undefined);
    const fnRef = useRef(fn);
    fnRef.current = fn;
    const mountedRef = useRef(true);
    useEffect(
        () => () => {
            mountedRef.current = false;
        },
        [],
    );

    const mutate = useCallback(async (...args: A): Promise<R> => {
        setPending(true);
        setError(undefined);
        try {
            return await fnRef.current(...args);
        } catch (e) {
            if (mountedRef.current) setError(e as Error);
            throw e;
        } finally {
            if (mountedRef.current) setPending(false);
        }
    }, []);

    return { mutate, pending, error };
}
