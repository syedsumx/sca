import type { ConsumerClient } from '../api/client';
import type { MessageKey } from '../i18n';

/** The roles first-run onboarding is shaped for (stored in the profile's `ui.onboarding.role`). */
export const ROLES = ['analyst', 'engineer', 'steward', 'admin', 'explorer'] as const;
export type Role = (typeof ROLES)[number];

export interface GuideUi {
    onboarding: { role: Role | null; done: string[]; dismissed: boolean; asked: boolean };
    sidebar: { pinned: string[]; hidden: string[]; show_all: boolean };
}

export const EMPTY_UI: GuideUi = {
    onboarding: { role: null, done: [], dismissed: false, asked: false },
    sidebar: { pinned: [], hidden: [], show_all: false },
};

/** Whatever the API answers (or nothing), as a complete state. */
export function normaliseUi(raw: unknown): GuideUi {
    const r = (raw && typeof raw === 'object' ? raw : {}) as Partial<GuideUi>;
    const ob = (r.onboarding ?? {}) as Partial<GuideUi['onboarding']>;
    const sb = (r.sidebar ?? {}) as Partial<GuideUi['sidebar']>;
    return {
        onboarding: {
            role: ROLES.includes(ob.role as Role) ? (ob.role as Role) : null,
            done: Array.isArray(ob.done) ? ob.done.filter((x) => typeof x === 'string') : [],
            dismissed: !!ob.dismissed,
            asked: !!ob.asked,
        },
        sidebar: {
            pinned: Array.isArray(sb.pinned) ? sb.pinned : [],
            hidden: Array.isArray(sb.hidden) ? sb.hidden : [],
            show_all: !!sb.show_all,
        },
    };
}

/** A free-text profile role ("Financial analyst") read as an onboarding role, to suggest it. */
export function roleFromProfile(text: string | null | undefined): Role | null {
    const s = (text || '').toLowerCase();
    if (!s.trim()) return null;
    if (/steward|governance|catalog|librarian|compliance|privacy/.test(s)) return 'steward';
    if (/admin|platform|operator|devops|\bit\b|security|sre/.test(s)) return 'admin';
    if (/engineer|developer|etl|pipeline|architect/.test(s)) return 'engineer';
    if (/analy|scientist|\bbi\b|report|finance|controller|marketing/.test(s)) return 'analyst';
    return null;
}

type Detect = (client: ConsumerClient, me: string) => Promise<boolean>;

export interface Step {
    id: string;
    title: MessageKey;
    hint: MessageKey;
    /** Where it is done (a route in the app). */
    to: string;
    /** How Cairn sees it was done, asking the API as the person (no detection: ticked by hand). */
    detect?: Detect;
    /** A route whose visit completes it (`/asset/` = any asset page). */
    visit?: string;
    /** An in-app event that completes it (`guideEvent('ask')`). */
    event?: string;
}

/** The items of a list answer, wherever the API put them. */
export function rowsOf(x: unknown): Record<string, unknown>[] {
    if (Array.isArray(x)) return x as Record<string, unknown>[];
    if (x && typeof x === 'object') {
        for (const v of Object.values(x as Record<string, unknown>)) if (Array.isArray(v)) return v as Record<string, unknown>[];
    }
    return [];
}

const get = (client: ConsumerClient, path: string) => client.request<unknown>('GET', path).then(rowsOf);
/** Any row (matching `pred`) in any of the lists; a list the person can't read counts as none. */
const any = (paths: string[], pred: (r: Record<string, unknown>, me: string) => boolean = () => true): Detect =>
    async (client, me) => {
        for (const p of paths) {
            try {
                if ((await get(client, p)).some((r) => pred(r, me))) return true;
            } catch {
                // not available here, or not allowed: not done by this route
            }
        }
        return false;
    };
const mine = (r: Record<string, unknown>, me: string) => r.owner === me || r.created_by === me || r.actor === me;

export const STEPS: Record<Role, Step[]> = {
    analyst: [
        { id: 'find-dataset', title: 'guide.step.findDataset', hint: 'guide.step.findDatasetHint', to: '/search', visit: '/asset/' },
        { id: 'run-query', title: 'guide.step.runQuery', hint: 'guide.step.runQueryHint', to: '/sql', detect: any(['/sql/history']) },
        { id: 'save-query', title: 'guide.step.saveQuery', hint: 'guide.step.saveQueryHint', to: '/sql', detect: any(['/sql/saved'], mine) },
        { id: 'build-dashboard', title: 'guide.step.buildDashboard', hint: 'guide.step.buildDashboardHint', to: '/dashboards',
            detect: any(['/sql/dashboards'], mine) },
        { id: 'ask-question', title: 'guide.step.askQuestion', hint: 'guide.step.askQuestionHint', to: '/assistant', event: 'ask' },
    ],
    engineer: [
        { id: 'connect-source', title: 'guide.step.connectSource', hint: 'guide.step.connectSourceHint', to: '/ingestion',
            detect: any(['/sources', '/v1/mirrors', '/v1/document-sources', '/v1/saas-syncs']) },
        { id: 'create-flow', title: 'guide.step.createFlow', hint: 'guide.step.createFlowHint', to: '/flows',
            detect: any(['/v1/flows', '/v1/pipes'], mine) },
        { id: 'schedule', title: 'guide.step.schedule', hint: 'guide.step.scheduleHint', to: '/flows',
            detect: any(['/v1/flows'], (r, me) => mine(r, me) && r.next_run_ms != null) },
        { id: 'quality-check', title: 'guide.step.qualityCheck', hint: 'guide.step.qualityCheckHint', to: '/lakehouse?tab=quality-monitors',
            detect: any(['/quality/monitors']) },
    ],
    steward: [
        { id: 'review-proposals', title: 'guide.step.reviewProposals', hint: 'guide.step.reviewProposalsHint', to: '/ontology?tab=review',
            detect: any(['/v1/ontology/proposals?status=ACCEPTED', '/v1/ontology/proposals?status=DISMISSED']) },
        { id: 'glossary-term', title: 'guide.step.glossaryTerm', hint: 'guide.step.glossaryTermHint', to: '/semantic?tab=ontology-terms' },
        { id: 'set-owner', title: 'guide.step.setOwner', hint: 'guide.step.setOwnerHint', to: '/search' },
        { id: 'review-requests', title: 'guide.step.reviewRequests', hint: 'guide.step.reviewRequestsHint', to: '/requests',
            detect: any(['/marketplace/access-requests?scope=inbox'], (r, me) => r.decided_by === me) },
    ],
    admin: [
        { id: 'sso', title: 'guide.step.sso', hint: 'guide.step.ssoHint', to: '/settings' },
        { id: 'groups', title: 'guide.step.groups', hint: 'guide.step.groupsHint', to: '/trust?tab=workspaces' },
        { id: 'policies', title: 'guide.step.policies', hint: 'guide.step.policiesHint', to: '/governance' },
        { id: 'ai-gateway', title: 'guide.step.aiGateway', hint: 'guide.step.aiGatewayHint', to: '/usage?tab=gateway-routes',
            detect: any(['/v1/ai/gateway/routes']) },
        { id: 'budgets', title: 'guide.step.budgets', hint: 'guide.step.budgetsHint', to: '/usage?tab=budgets', detect: any(['/v1/budgets']) },
    ],
    explorer: [
        { id: 'discover', title: 'guide.step.discover', hint: 'guide.step.discoverHint', to: '/discover', visit: '/discover' },
        { id: 'find-dataset', title: 'guide.step.findDataset', hint: 'guide.step.findDatasetHint', to: '/search', visit: '/asset/' },
        { id: 'marketplace', title: 'guide.step.marketplace', hint: 'guide.step.marketplaceHint', to: '/marketplace', visit: '/marketplace' },
        { id: 'ask-question', title: 'guide.step.askQuestion', hint: 'guide.step.askQuestionHint', to: '/assistant', event: 'ask' },
    ],
};

export interface Progress {
    steps: (Step & { done: boolean })[];
    done: number;
    total: number;
    complete: boolean;
}

export function progress(role: Role | null, done: string[]): Progress {
    const steps = (role ? STEPS[role] : []).map((s) => ({ ...s, done: done.includes(s.id) }));
    const n = steps.filter((s) => s.done).length;
    return { steps, done: n, total: steps.length, complete: steps.length > 0 && n === steps.length };
}

/** The steps of `role` that a visit to `pathname` completes. */
export function stepsVisited(role: Role | null, pathname: string): string[] {
    if (!role) return [];
    return STEPS[role].filter((s) => s.visit && (s.visit.endsWith('/') ? pathname.startsWith(s.visit) : pathname === s.visit))
        .map((s) => s.id);
}

/** The steps of `role` an in-app event completes. */
export function stepsForEvent(role: Role | null, event: string): string[] {
    return role ? STEPS[role].filter((s) => s.event === event).map((s) => s.id) : [];
}

/** Ask the API which of the role's open steps are done (each check as the person; a failing one is not done). */
export async function detectDone(client: ConsumerClient, me: string, role: Role | null, done: string[]): Promise<string[]> {
    if (!role) return [];
    const open = STEPS[role].filter((s) => s.detect && !done.includes(s.id));
    const found = await Promise.all(open.map((s) => s.detect!(client, me).catch(() => false)));
    return open.filter((_, i) => found[i]).map((s) => s.id);
}

/** `done` with `ids` added, in step order (unchanged array when nothing is new). */
export function withDone(done: string[], ids: string[]): string[] {
    const add = ids.filter((id) => !done.includes(id));
    return add.length ? [...done, ...add] : done;
}

/** Tell the guide something happened in the app (the assistant answered: `ask`). */
export function guideEvent(name: string): void {
    try {
        window.dispatchEvent(new CustomEvent('cairn:guide', { detail: name }));
    } catch {
        // no window (tests outside a DOM)
    }
}
