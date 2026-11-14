/**
 * Search state lives in the URL: `?q=revenue&f.domains=urn:li:domain:finance&f._entityType=DATASET`.
 * Multi-valued facets repeat the parameter. This makes searches shareable and back/forward-safe.
 */

export interface SearchState {
    query: string;
    filters: Record<string, string[]>;
    page: number;
}

const PREFIX = 'f.';

export function parseSearchState(search: string): SearchState {
    const params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search);
    const filters: Record<string, string[]> = {};
    params.forEach((value, key) => {
        if (key.startsWith(PREFIX) && value) {
            const field = key.slice(PREFIX.length);
            (filters[field] ??= []).push(value);
        }
    });
    Object.keys(filters).forEach((k) => {
        filters[k] = Array.from(new Set(filters[k])).sort();
    });
    const page = Math.max(1, parseInt(params.get('page') || '1', 10) || 1);
    return { query: params.get('q') ?? '', filters, page };
}

export function serializeSearchState(state: SearchState): string {
    const params = new URLSearchParams();
    if (state.query) params.set('q', state.query);
    Object.keys(state.filters)
        .sort()
        .forEach((field) => {
            Array.from(new Set(state.filters[field]))
                .sort()
                .forEach((v) => params.append(`${PREFIX}${field}`, v));
        });
    if (state.page > 1) params.set('page', String(state.page));
    const s = params.toString();
    return s ? `?${s}` : '';
}

export function toggleFilter(state: SearchState, field: string, value: string): SearchState {
    const current = state.filters[field] ?? [];
    const next = current.includes(value) ? current.filter((v) => v !== value) : [...current, value];
    const filters = { ...state.filters };
    if (next.length) filters[field] = next.sort();
    else delete filters[field];
    return { ...state, filters, page: 1 };
}

export function activeFilterCount(state: SearchState): number {
    return Object.values(state.filters).reduce((n, v) => n + v.length, 0);
}
