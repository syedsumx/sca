import React, { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef } from 'react';

import { ConsumerClient, ConsumerClientOptions } from './client';
import { QueryCache, defaultCache, optimistic } from './cache';

interface ConsumerContextValue {
    client: ConsumerClient;
    cache: QueryCache;
}

const ConsumerContext = createContext<ConsumerContextValue | null>(null);

export interface ConsumerApiProviderProps extends ConsumerClientOptions {
    client?: ConsumerClient;
    cache?: QueryCache;
    children?: React.ReactNode;
}

export function ConsumerApiProvider({ client, cache, children, ...options }: ConsumerApiProviderProps) {
    const value = useMemo<ConsumerContextValue>(
        () => ({ client: client ?? new ConsumerClient(options), cache: cache ?? defaultCache }),
        // eslint-disable-next-line react-hooks/exhaustive-deps
        [client, cache, options.baseUrl, options.graphqlUrl],
    );
    return React.createElement(ConsumerContext.Provider, { value }, children);
}

let fallback: ConsumerContextValue | null = null;

export function useConsumerApi(): ConsumerContextValue {
    const ctx = useContext(ConsumerContext);
    if (ctx) return ctx;
    if (!fallback) fallback = { client: new ConsumerClient(), cache: defaultCache };
    return fallback;
}

export function useConsumerClient(): ConsumerClient {
    return useConsumerApi().client;
}

export interface QueryState<T> {
    data: T | undefined;
    error: Error | undefined;
    loading: boolean;
    refresh: () => Promise<void>;
    mutate: (updater: T | ((prev: T | undefined) => T)) => void;
}

/**
 * Stale-while-revalidate query hook. `key === null` disables the query. Cached data is returned
 * immediately; a background refetch runs when the entry is stale (older than `staleMs` or
 * invalidated).
 */
export function useDheQuery<T>(
    key: string | null,
    loader: (client: ConsumerClient, signal: AbortSignal) => Promise<T>,
    options: { staleMs?: number; pollMs?: number } = {},
): QueryState<T> {
    const { client, cache } = useConsumerApi();
    const staleMs = options.staleMs ?? 30_000;
    const [, force] = useReducer((x: number) => x + 1, 0);
    const loaderRef = useRef(loader);
    loaderRef.current = loader;
    const mounted = useRef(true);

    const load = useCallback(async () => {
        if (!key) return;
        const controller = new AbortController();
        try {
            await cache.fetch<T>(key, () => loaderRef.current(client, controller.signal));
        } catch {
            // error stored in cache entry
        }
    }, [key, cache, client]);

    useEffect(() => {
        mounted.current = true;
        if (!key) return undefined;
        const unsubscribe = cache.subscribe(key, () => {
            if (!mounted.current) return;
            force();
            const entry = cache.get(key);
            if (entry?.stale && !entry.promise && !entry.error) void load();
        });
        if (cache.isStale(key, staleMs)) void load();
        let timer: ReturnType<typeof setInterval> | undefined;
        if (options.pollMs) {
            timer = setInterval(() => {
                cache.invalidate(key);
            }, options.pollMs);
        }
        return () => {
            mounted.current = false;
            unsubscribe();
            if (timer) clearInterval(timer);
        };
    }, [key, cache, staleMs, load, options.pollMs]);

    const entry = key ? cache.get<T>(key) : undefined;
    const refresh = useCallback(async () => {
        if (!key) return;
        cache.invalidate(key);
        await load();
    }, [key, cache, load]);
    const mutate = useCallback(
        (updater: T | ((prev: T | undefined) => T)) => {
            if (key) cache.set<T>(key, updater);
        },
        [key, cache],
    );
    return {
        data: entry?.data,
        error: entry?.data === undefined ? entry?.error : undefined,
        loading: !!key && entry?.data === undefined && !entry?.error,
        refresh,
        mutate,
    };
}

/** Returns a function that runs an optimistic update against the shared cache. */
export function useOptimistic() {
    const { cache } = useConsumerApi();
    return useCallback(
        <T, R>(key: string, updater: (prev: T | undefined) => T, request: () => Promise<R>) =>
            optimistic<T, R>(cache, key, updater, request),
        [cache],
    );
}

/** Debounce a value (search-as-you-type). */
export function useDebounced<T>(value: T, ms = 200): T {
    const [state, setState] = React.useState(value);
    useEffect(() => {
        const t = setTimeout(() => setState(value), ms);
        return () => clearTimeout(t);
    }, [value, ms]);
    return state;
}
