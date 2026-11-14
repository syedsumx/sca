/**
 * The consumer UI's one error presenter: any error from Cairn's API as a sentence a person understands, what to do
 * about it, and the technical detail kept for "Details".
 */
import { detectLocale, translate } from '../i18n';
import type { MessageKey } from '../i18n/messages';

type T = (key: MessageKey, vars?: Record<string, string | number>) => string;

export type ErrorKind =
    | 'forbidden' | 'unauthorized' | 'notFound' | 'notConnected' | 'conflict' | 'invalid' | 'rate' | 'unconfigured'
    | 'unavailable' | 'network' | 'server';

export interface PresentedError {
    kind: ErrorKind;
    status?: number;
    title: string;
    hint: string;
    /** The docs section that says how to turn the feature on (not configured). */
    docs?: string;
    /** What the API said, with the status and path: for "Details" and to copy. */
    detail: string;
    /** Not connected: where it is connected (an in-app route, or a docs page under /docs/). */
    connect?: { to: string; label: string };
}

/**
 * The services and integrations an area reads from, by API path. When one is not set up on this Cairn its API is not
 * mounted, so every call answers FastAPI's own 404 ("Not Found"); that is "not connected yet", not a missing object.
 * `connect` is where it is connected: a page of the workspace, else its section of the operator manual.
 */
export const SERVICES: { path: RegExp; name: MessageKey; connect: string }[] = [
    { path: /^\/v1\/airflow\b/, name: 'svc.airflow', connect: '/docs/administration.html#managed-airflow' },
    { path: /^\/v1\/nifi\b/, name: 'svc.nifi', connect: '/docs/administration.html#managed-nifi' },
    { path: /^\/v1\/observability\b|^\/status\.json$/, name: 'svc.observability', connect: '/docs/administration.html#observability' },
    { path: /^\/v1\/people\b|^\/scim\b/, name: 'svc.people', connect: '/docs/administration.html#user-provisioning-scim' },
    { path: /^\/v1\/ml\b/, name: 'svc.mlflow', connect: '/docs/administration.html#machine-learning-mlflow-behind-cairn' },
    { path: /^\/v1\/jobs\b/, name: 'svc.jobs', connect: '/docs/administration.html#scheduled-notebook-and-python-jobs' },
    { path: /^\/governance\/audit\b/, name: 'svc.audit', connect: '/docs/administration.html#audit-log' },
    { path: /^\/v1\/system-tables\b/, name: 'svc.systemTables', connect: '/docs/administration.html#system-tables' },
    { path: /^\/v1\/dataplanes\b|^\/v1\/network\b|^\/v1\/compute\b|^\/v1\/app-installs\b/, name: 'svc.compute',
        connect: '/compute' },
    { path: /^\/marketplace\/publications\b/, name: 'svc.publications', connect: '/sharing' },
    { path: /^\/sql\/(reports|metric-alerts)\b/, name: 'svc.sqlWorkspace', connect: '/docs/administration.html#sql-workspace' },
];

/** The service a request path belongs to, if it is one an admin connects. */
export function serviceOf(path: string | undefined): (typeof SERVICES)[number] | undefined {
    const p = (path ?? '').replace(/^\/cairn\/api/, '').split('?')[0];
    return SERVICES.find((s) => s.path.test(p));
}

/** A 404 that means "this API is not mounted here" (the service is not set up), not "this object is gone". */
export function isNotConnected(e: unknown): boolean {
    const err = (e ?? {}) as ApiErrorLike;
    if (errorStatus(e) !== 404) return false;
    const said = apiDetail(err).trim();
    // FastAPI's own answer for a route nobody mounted, or the demo's for an API it does not carry.
    return /^not found$/i.test(said) || /^Not available in the demo\b/.test(said);
}

export interface ErrorContext {
    /** The action being taken, as its button says ("New budget"); none: the area was being read. */
    action?: string;
    /** The area, as its tab says ("Budgets"). */
    area?: string;
    /** The area's docs page and anchor (`administration.html#cost-budgets`). */
    docs?: string;
}

interface ApiErrorLike {
    status?: number;
    path?: string;
    body?: unknown;
    message?: string;
}

/** The API's own words: FastAPI's `detail` (a sentence, or 422's list), else the message. */
export function apiDetail(e: ApiErrorLike): string {
    const body = e.body as { detail?: unknown } | string | undefined;
    const d = body && typeof body === 'object' ? body.detail : typeof body === 'string' ? body : undefined;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) {
        return d.map((x) => (x && typeof x === 'object' && 'msg' in x
            ? `${Array.isArray((x as { loc?: unknown }).loc) ? `${((x as { loc: unknown[] }).loc).filter((p) => p !== 'body').join('.')}: ` : ''}${String((x as { msg: unknown }).msg)}`
            : String(x))).join('; ');
    }
    if (d !== undefined) return JSON.stringify(d);
    return e.message ?? String(e);
}

const NOT_CONFIGURED = /not configured|not enabled|is off\b|turned off|disabled on this|set CAIRN_[A-Z_]+|no .* configured/i;

export function errorStatus(e: unknown): number | undefined {
    const s = (e as ApiErrorLike | undefined)?.status;
    if (typeof s === 'number') return s;
    const m = /\bHTTP (\d{3})\b/.exec((e as Error | undefined)?.message ?? '');
    return m ? Number(m[1]) : undefined;
}

export function presentError(e: unknown, t: T, ctx: ErrorContext = {}): PresentedError {
    const err = (e ?? {}) as ApiErrorLike;
    const status = errorStatus(e);
    const said = apiDetail(err);
    const detail = [status !== undefined ? `HTTP ${status}` : null, err.path ?? null].filter(Boolean).join(' · ') +
        (said ? `${status !== undefined || err.path ? '\n' : ''}${said}` : '');
    const area = ctx.area ?? t('err.thisArea');
    const base = { status, detail };
    if (status === 0 || (status === undefined && /network|failed to fetch/i.test(said))) {
        return { ...base, kind: 'network', title: t('err.network.title'), hint: t('err.network.hint') };
    }
    if (status === 401) return { ...base, kind: 'unauthorized', title: t('err.unauthorized.title'), hint: t('err.unauthorized.hint') };
    if (status === 403) {
        const privilege = /\b([A-Z][A-Z_]{4,}[A-Z])\b/.exec(said)?.[1];
        const title = ctx.action ? t('err.forbidden.title', { action: ctx.action }) : t('err.forbidden.view', { area });
        // When the API says where the privilege is needed ("... on X to move it there"), that sentence is shown too: the
        // privilege's name alone tells someone who holds it elsewhere nothing.
        const words = privilege ? said.replace(privilege, '').trim().split(/\s+/).filter(Boolean).length : 0;
        const where = privilege && words >= 5 && said.length <= 240 && !/[{}<>]/.test(said) ? ` ${said}` : '';
        const hint = t('err.forbidden.hint', { area }) + (privilege ? ` ${t('err.forbidden.needs', { privilege })}` : '') + where;
        return { ...base, kind: 'forbidden', title, hint };
    }
    if (status === 404 && isNotConnected(e)) {
        const svc = serviceOf(err.path);
        const service = svc ? t(svc.name) : ctx.area ?? t('svc.thisService');
        return { ...base, kind: 'notConnected', title: t('err.notConnected.title', { service }),
            hint: t('err.notConnected.hint', { service }),
            connect: { to: svc?.connect ?? (ctx.docs ? `/docs/${ctx.docs}` : '/docs/administration.html'),
                label: t('err.notConnected.connect') } };
    }
    if (status === 404) return { ...base, kind: 'notFound', title: t('err.notFound.title'), hint: t('err.notFound.hint') };
    if (status === 409) return { ...base, kind: 'conflict', title: t('err.conflict.title'), hint: said || t('err.conflict.hint') };
    if (status === 400 || status === 422) {
        return { ...base, kind: 'invalid', title: t('err.invalid.title'),
            hint: status === 400 && said && said.length < 300 ? said : t('err.invalid.hint') };
    }
    if (status === 429) return { ...base, kind: 'rate', title: t('err.rate.title'), hint: t('err.rate.hint') };
    if ((status === 503 || status === 501) && NOT_CONFIGURED.test(said)) {
        return { ...base, kind: 'unconfigured', title: t('err.unconfigured.title'), hint: t('err.unconfigured.hint'),
            docs: ctx.docs ? `/docs/${ctx.docs}` : '/docs/administration.html' };
    }
    if (status === 503 || status === 502 || status === 504) {
        return { ...base, kind: 'unavailable', title: t('err.unavailable.title', { area }), hint: t('err.unavailable.hint') };
    }
    return { ...base, kind: 'server', title: t('err.server.title'), hint: t('err.server.hint') };
}

/**
 * The presenter's sentence where there is no room for a panel (a toast, a line in a list): the title and what to do,
 * in the person's language. The API's words stay for the presenter's "Details"; only a 400's short sentence and a
 * 403's privilege, which say what to change or ask for, are part of the hint.
 */
export function describeError(e: unknown, ctx: ErrorContext = {}): string {
    const t: T = (key, vars) => translate(detectLocale(), key, vars);
    const p = presentError(e, t, ctx);
    return `${p.title}. ${p.kind === 'server' ? t('err.server.short') : p.hint}`;
}
