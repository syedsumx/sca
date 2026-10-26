/**
 * Minimal stale-while-revalidate cache for Cairn server state.
 *
 * - entries keyed by string (by convention the request path + query, e.g. "/collab/bookmarks")
 * - concurrent loads of the same key are de-duplicated
 * - `invalidate(prefix)` marks matching entries stale and notifies subscribers to refetch
 * - `set(key, updater)` supports optimistic updates; `optimistic()` adds automatic rollback
 */

export interface CacheEntry<T = unknown> {
    data?: T;
    error?: Error;
    updatedAt: number;
    stale: boolean;
    promise?: Promise<T>;
}

type Listener = () => void;

export class QueryCache {
    private readonly entries = new Map<string, CacheEntry>();

    private readonly listeners = new Map<string, Set<Listener>>();

    get<T>(key: string): CacheEntry<T> | undefined {
        return this.entries.get(key) as CacheEntry<T> | undefined;
    }

    subscribe(key: string, listener: Listener): () => void {
        let set = this.listeners.get(key);
        if (!set) {
            set = new Set();
            this.listeners.set(key, set);
        }
        set.add(listener);
        return () => {
            set?.delete(listener);
        };
    }

    private notify(key: string): void {
        this.listeners.get(key)?.forEach((l) => l());
    }

    /** True when the entry is missing, marked stale, or older than `staleMs`. */
    isStale(key: string, staleMs: number, now = Date.now()): boolean {
        const e = this.entries.get(key);
        if (!e || e.stale || e.data === undefined) return true;
        return now - e.updatedAt > staleMs;
    }

    fetch<T>(key: string, loader: () => Promise<T>): Promise<T> {
        const existing = this.entries.get(key) as CacheEntry<T> | undefined;
        if (existing?.promise) return existing.promise;
        const promise = loader().then(
            (data) => {
                this.entries.set(key, { data, updatedAt: Date.now(), stale: false });
                this.notify(key);
                return data;
            },
            (error: Error) => {
                const prev = this.entries.get(key);
                this.entries.set(key, { ...prev, error, promise: undefined, updatedAt: prev?.updatedAt ?? 0, stale: true });
                this.notify(key);
                throw error;
            },
        );
        this.entries.set(key, { ...(existing ?? { updatedAt: 0, stale: true }), promise } as CacheEntry);
        return promise;
    }

    set<T>(key: string, updater: T | ((prev: T | undefined) => T)): void {
        const prev = this.entries.get(key) as CacheEntry<T> | undefined;
        const data =
            typeof updater === 'function' ? (updater as (p: T | undefined) => T)(prev?.data) : (updater as T);
        this.entries.set(key, { data, updatedAt: Date.now(), stale: prev?.stale ?? false, error: undefined });
        this.notify(key);
    }

    /** Mark every key starting with `prefix` stale and notify, so mounted hooks refetch. */
    invalidate(prefix: string): number {
        let n = 0;
        this.entries.forEach((entry, key) => {
            if (key.startsWith(prefix)) {
                entry.stale = true;
                n += 1;
                this.notify(key);
            }
        });
        return n;
    }

    clear(): void {
        this.entries.clear();
    }
}

/**
 * Apply `updater` to `key` immediately, run `request`, and roll back to the previous value if it
 * fails. The error is re-thrown so callers can surface it (toast).
 */
export async function optimistic<T, R>(
    cache: QueryCache,
    key: string,
    updater: (prev: T | undefined) => T,
    request: () => Promise<R>,
): Promise<R> {
    const snapshot = cache.get<T>(key)?.data;
    cache.set<T>(key, updater);
    try {
        return await request();
    } catch (e) {
        cache.set<T>(key, snapshot as T);
        throw e;
    }
}

export const defaultCache = new QueryCache();
