/**
 * The consumer UI's one error presenter: any error from Cairn's API as a sentence a person understands, what to do
 * about it, and the technical detail kept for "Details".
 */
import type { MessageKey } from '../i18n/messages';

type T = (key: MessageKey, vars?: Record<string, string | number>) => string;

export type ErrorKind =
    | 'forbidden' | 'unauthorized' | 'notFound' | 'conflict' | 'invalid' | 'rate' | 'unconfigured' | 'unavailable'
    | 'network' | 'server';

export interface PresentedError {
    kind: ErrorKind;
    status?: number;
    title: string;
    hint: string;
    /** The docs section that says how to turn the feature on (not configured). */
    docs?: string;
    /** What the API said, with the status and path: for "Details" and to copy. */
    detail: string;
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
        const hint = t('err.forbidden.hint', { area }) + (privilege ? ` ${t('err.forbidden.needs', { privilege })}` : '');
        return { ...base, kind: 'forbidden', title, hint };
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
