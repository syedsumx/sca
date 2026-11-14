import type { IconName } from '../components/Icons';

/**
 * The icon of each main page, shown in its header's tile (L150: every main page has one, the same as its sidebar entry;
 * a test checks they match Layout's NAV_GROUPS). Deep pages (an asset, a glossary term) have none.
 */
export const PAGE_ICONS: Record<string, IconName> = {
    '/discover': 'home', '/catalog': 'catalog', '/products': 'cube', '/glossary': 'tag', '/domains': 'domain',
    '/semantics': 'layers', '/lineage': 'lineage', '/requests': 'inbox',
    '/governance': 'shield', '/people': 'user', '/automation': 'repeat', '/security': 'lock', '/sharing': 'share',
    '/usage': 'coin', '/reports': 'report',
    '/quality': 'contract', '/runs': 'play', '/incidents': 'bell', '/observability': 'pulse', '/alerts': 'bell',
    '/sql': 'code', '/sql/workbooks': 'sheet', '/dashboards': 'chart', '/sql/input-tables': 'table',
    '/ingestion': 'download', '/lakehouse': 'database', '/flows': 'flow',
    '/assistant': 'sparkle', '/agents': 'bot', '/ml': 'brain', '/ai/gateway': 'server',
    '/compute': 'server', '/settings': 'gear', '/capabilities': 'grid',
    '/inbox': 'inbox', '/profile': 'user', '/get-started': 'sparkle', '/extensions': 'puzzle',
};

/** The icon of a main page, by its path; none for deep pages. */
export function iconForPath(pathname: string): IconName | undefined {
    return PAGE_ICONS[pathname.replace(/\/+$/, '') || '/'];
}

/** A page's introduction in one line: its first sentence (the rest is in the "?" help). */
export function firstSentence(text: string): string {
    const t = text.trim();
    // The first sentence end, or the first colon that starts a list ("…, and why: who…"), whichever comes first.
    const m = /[.!?](\s|$)|:\s/.exec(t);
    if (!m) return t;
    return m[0].startsWith(':') ? `${t.slice(0, m.index)}.` : t.slice(0, m.index + 1);
}

/** The full introductions of the pages on screen, for their help panel (a page's header writes it; the panel reads it). */
const INTROS = new Map<string, string>();
export function rememberIntro(pathname: string, text: string): void {
    INTROS.set(pathname, text);
}
export function introFor(pathname: string): string | undefined {
    return INTROS.get(pathname);
}
