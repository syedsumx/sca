import { useDheQuery } from '../api/hooks';
import type { Hub } from '../resources/types';

/** `GET /v1/me/capabilities`: the platform privileges the person holds, decided by the policy engine. */
export interface Capabilities {
    actor: string;
    is_admin: boolean;
    privileges: Record<string, boolean>;
    features?: Record<string, boolean>;
}

/**
 * Areas whose list the API refuses without a platform privilege (read from the routers: each needs any one of
 * these). Hiding is a convenience: the API still decides every request.
 */
export const TAB_REQUIRES: Record<string, string[]> = {
    residency: ['MANAGE_POLICIES'],
    tokens: ['MANAGE_POLICIES'],
    siem: ['MANAGE_POLICIES'],
    storage: ['MANAGE_POLICIES'],
    samples: ['MANAGE_SYSTEM_OPERATIONS'],
    posture: ['MANAGE_SYSTEM_OPERATIONS'],
    limits: ['MANAGE_SYSTEM_OPERATIONS'],
    'ai-usage': ['MANAGE_SYSTEM_OPERATIONS'],
    chargeback: ['MANAGE_SYSTEM_OPERATIONS'],
    'chargeback-cost-centres': ['MANAGE_SYSTEM_OPERATIONS'],
    'ai-gateway-models': ['MANAGE_SYSTEM_OPERATIONS'],
    maintenance: ['MANAGE_SYSTEM_OPERATIONS'],
    'maintenance-runs': ['MANAGE_SYSTEM_OPERATIONS'],
    'notification-routes': ['MANAGE_GLOBAL_SETTINGS'],
};

/** Purpose-built pages that are for Cairn's operators only. */
export const PAGE_REQUIRES: Record<string, string[]> = {
    '/governance': ['MANAGE_POLICIES'],
};

export type CapabilityState = { caps: Capabilities | undefined; loading: boolean; error: boolean };

/**
 * Whether a person holding `caps` may use something that needs any one of `needs`. Fail closed: while the answer
 * is unknown (loading, or the endpoint failed) only what needs nothing is offered.
 */
export function holds(needs: string[] | undefined, state: CapabilityState): boolean {
    if (!needs || needs.length === 0) return true;
    if (!state.caps) return false;
    return needs.some((p) => state.caps?.privileges?.[p] === true);
}

/** A hub's tabs the person may use. */
export function visibleTabs(hub: Hub, state: CapabilityState, known: (key: string) => boolean = () => true): string[] {
    return hub.tabs.filter((k) => known(k) && holds(TAB_REQUIRES[k], state));
}

/** Whether a navigation entry is offered: a page's own requirement, and for a hub at least one usable tab. */
export function routeAllowed(to: string, hubs: Hub[], state: CapabilityState, known?: (key: string) => boolean): boolean {
    if (!holds(PAGE_REQUIRES[to], state)) return false;
    const hub = hubs.find((h) => h.route === to);
    return hub ? visibleTabs(hub, state, known).length > 0 : true;
}

/** The signed-in person's capabilities (fetched once, kept five minutes). */
export function useCapabilities(): CapabilityState {
    const q = useDheQuery<Capabilities>('/v1/me/capabilities', (c, signal) =>
        c.request<Capabilities>('GET', '/v1/me/capabilities', undefined, { signal }), { staleMs: 300_000 });
    return { caps: q.data, loading: q.loading, error: !!q.error };
}
