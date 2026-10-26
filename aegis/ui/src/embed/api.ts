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
 * Script tag `data-aegis-base`, else derived from the SDK script URL (…/<base>/embed/v1/sdk.js), else
 * '/aegis/api'. `document.currentScript` is only set while the script first executes, so this is
 * computed once at load time.
 */
export function computeBaseUrl(script: HTMLScriptElement | null): string {
    if (script?.dataset.aegisBase) return script.dataset.aegisBase;
    if (script?.src) {
        const u = new URL(script.src);
        const idx = u.pathname.indexOf('/embed/v1/');
        return `${u.origin}${idx >= 0 ? u.pathname.slice(0, idx) : '/aegis/api'}`;
    }
    return '/aegis/api';
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
    // Accept both the API base (…/aegis/api) and the embed base (…/aegis/api/embed/v1) the backend emits.
    const root = baseUrl.replace(/\/+$/, '').replace(/\/embed\/v1$/, '');
    const res = await fetchImpl(`${root}/embed/v1${path}?${params.toString()}`, {
        headers: { Accept: 'application/json' },
        credentials: 'omit',
    });
    if (!res.ok) {
        const msg = res.status === 401 || res.status === 403 ? 'This embed link has expired or was revoked.' : `Unavailable (${res.status})`;
        throw new EmbedError(msg, res.status);
    }
    return (await res.json()) as T;
}
