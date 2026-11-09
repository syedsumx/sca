/** Read-only, token-scoped embed API (`/embed/v1/*`). No cookies are sent: embeds run in BI tools. */

export interface EmbedAssetCard {
    urn: string;
    name: string;
    type: string;
    platform: string | null;
    description: string | null;
    owners: string[];
    certification: string | null;
    quality_status: string | null;
    freshness: { last_updated: string | null };
    url: string;
}

export interface EmbedQuality {
    urn: string;
    status: string | null;
    monitors: { name: string; type: string; status: string; at: string | null }[];
}

export interface EmbedLineageNode {
    urn: string;
    name: string;
    type: string;
    platform: string | null;
    quality_status: string | null;
    hidden?: boolean;
}

export interface EmbedLineage {
    nodes: EmbedLineageNode[];
    edges: { source: string; target: string }[];
    hidden_count: number;
}

export class EmbedError extends Error {
    readonly status: number;

    constructor(message: string, status: number) {
        super(message);
        this.status = status;
    }
}

/**
 * A failed load as a sentence for the host page's readers. The embed bundle has no dependencies, so it keeps its own
 * short version of the consumer UI's error presenter (`consumer/utils/errors.ts`) rather than the API's words.
 */
export function embedErrorText(e: unknown): string {
    // embedFetch words its own errors by status; anything else is the network or the host page.
    return e instanceof EmbedError ? e.message : 'Cairn could not be reached. Check your connection, then try again.';
}

function embedStatusText(status: number): string {
    if (status === 401 || status === 403) return 'This embed link has expired or was revoked.';
    if (status === 404) return 'This was not found in Cairn. It may have been renamed or removed.';
    if (status === 429) return 'Too many requests right now. Try again in a minute.';
    return `Cairn could not load this right now (${status}). Try again in a minute.`;
}

/**
 * Script tag `data-cairn-base`, else derived from the SDK script URL (…/<base>/embed/v1/sdk.js), else
 * '/cairn/api'. `document.currentScript` is only set while the script first executes, so this is
 * computed once at load time.
 */
export function computeBaseUrl(script: HTMLScriptElement | null): string {
    if (script?.dataset.cairnBase) return script.dataset.cairnBase;
    if (script?.src) {
        const u = new URL(script.src);
        const idx = u.pathname.indexOf('/embed/v1/');
        return `${u.origin}${idx >= 0 ? u.pathname.slice(0, idx) : '/cairn/api'}`;
    }
    return '/cairn/api';
}

const LOAD_TIME_BASE = computeBaseUrl(
    typeof document !== 'undefined' ? (document.currentScript as HTMLScriptElement | null) : null,
);

export function defaultBaseUrl(): string {
    return LOAD_TIME_BASE;
}

export async function embedFetch<T>(
    baseUrl: string,
    path: string,
    token: string,
    query: Record<string, string> = {},
    fetchImpl: typeof fetch = fetch,
): Promise<T> {
    const params = new URLSearchParams({ ...query, token });
    // Accept both the API base (…/cairn/api) and the embed base (…/cairn/api/embed/v1) the backend emits.
    const root = baseUrl.replace(/\/+$/, '').replace(/\/embed\/v1$/, '');
    const res = await fetchImpl(`${root}/embed/v1${path}?${params.toString()}`, {
        headers: { Accept: 'application/json' },
        credentials: 'omit',
    });
    if (!res.ok) {
        throw new EmbedError(embedStatusText(res.status), res.status);
    }
    return (await res.json()) as T;
}
