import { Spin } from 'antd';
import React, { useEffect, useImperativeHandle, useMemo, useState } from 'react';
import { useHistory } from 'react-router-dom';
import styled from 'styled-components';

import { useConsumerApi, useDebounced } from '../api/hooks';
import { assetPath } from '../components/Assets';
import { tokens } from '../components/tokens';
import { useDheSession } from '../context/CairnProvider';
import { MessageKey, useT } from '../i18n';
import { TAB_SPECS as RESOURCE_SPECS } from '../resources/customTabs';
import { HUBS, PAGE_AREAS } from '../resources/hubs';
import { createAction } from '../resources/ResourceView';
import { visibleTabs } from './capabilities';
import { useGuide } from './GuideProvider';
import { ROLE_PINS } from './nav';
import { PaletteItem, moveActive, pushRecent, rank, readRecent } from './palette';

const Panel = styled.div`
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    width: max(100%, min(560px, calc(100vw - 32px)));
    z-index: 1050;
    padding: 8px;
    background: ${tokens.color.surface};
    border: 1px solid ${tokens.color.border};
    border-radius: ${tokens.radius.md};
    box-shadow: ${tokens.shadow.hover};
    color: ${tokens.color.text};
    @media (max-width: 720px) {
        position: fixed;
        top: 60px;
        left: 8px;
        right: 8px;
        width: auto;
    }
`;

const List = styled.ul`
    list-style: none;
    margin: 0;
    padding: 0;
    max-height: min(60vh, 460px);
    overflow-y: auto;
    li {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 8px 10px;
        border-radius: ${tokens.radius.sm};
        cursor: pointer;
    }
    li[aria-selected='true'] {
        background: ${tokens.color.primarySoft};
        outline: 2px solid ${tokens.color.focus};
        outline-offset: -2px;
    }
    .label {
        flex: 1;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .detail {
        color: ${tokens.color.textMuted};
        margin-left: 6px;
    }
    .kind {
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: ${tokens.color.textMuted};
        flex: none;
    }
    kbd {
        font: 11px ui-monospace, SFMono-Regular, Menlo, monospace;
        border: 1px solid ${tokens.color.border};
        border-radius: 5px;
        padding: 0 5px;
        color: ${tokens.color.textMuted};
    }
`;


// The palette is drawn outside the workspace's root (a portal): its own visually-hidden style.
const SR_ONLY: React.CSSProperties = { position: 'absolute', width: 1, height: 1, padding: 0, margin: -1, overflow: 'hidden',
    clip: 'rect(0, 0, 0, 0)', whiteSpace: 'nowrap', border: 0 };

const KIND_KEYS: Record<PaletteItem['kind'], MessageKey> = {
    page: 'palette.kind.page', tab: 'palette.kind.tab', action: 'palette.kind.action', recent: 'palette.kind.recent',
    catalog: 'palette.kind.catalog', ask: 'palette.kind.ask', search: 'palette.kind.catalog',
};

export interface PaletteNavEntry {
    to: string;
    label: string;
}

/** Everything the search offers besides the catalog's answers: pages, hub tabs, actions and recent pages. */
export function paletteItems(nav: PaletteNavEntry[], t: (k: MessageKey, v?: Record<string, string | number>) => string,
    tabsOf: (route: string) => string[], role: keyof typeof ROLE_PINS | null, openShortcuts: () => void): PaletteItem[] {
    const boost = new Set(role ? ROLE_PINS[role] : []);
    const items: PaletteItem[] = nav.map((n) => ({ id: `page:${n.to}`, kind: 'page', label: n.label, to: n.to, boost: boost.has(n.to) ? 40 : 0 }));
    for (const hub of HUBS) {
        for (const key of tabsOf(hub.route)) {
            const spec = RESOURCE_SPECS[key];
            items.push({ id: `tab:${key}`, kind: 'tab', label: `${hub.title} › ${spec.title}`, keywords: spec.description,
                to: `${hub.route}?tab=${key}` });
            const make = createAction(spec);
            if (make) {
                const label = /^(new|create|add|register|connect)\b/i.test(make.label) ? make.label : t('palette.create', { what: spec.title });
                items.push({ id: `create:${key}`, kind: 'action', label, detail: `${hub.title} › ${spec.title}`,
                    keywords: `create new add ${spec.title}`,
                    to: `${hub.route}?tab=${key}&action=${encodeURIComponent(make.label)}` });
            }
        }
    }
    // Areas that are tabs of purpose-built pages (L149), where the page is offered.
    for (const page of PAGE_AREAS.filter((pg) => nav.some((n) => n.to === pg.route))) {
        for (const { tab, spec: key } of page.areas) {
            const spec = RESOURCE_SPECS[key];
            if (!spec) continue;
            items.push({ id: `tab:${page.route}:${tab}`, kind: 'tab', label: `${page.title} › ${spec.title}`, keywords: spec.description,
                to: `${page.route}?tab=${tab}` });
            const make = createAction(spec);
            if (make) {
                const label = /^(new|create|add|register|connect)\b/i.test(make.label) ? make.label : t('palette.create', { what: spec.title });
                items.push({ id: `create:${page.route}:${tab}`, kind: 'action', label, detail: `${page.title} › ${spec.title}`,
                    keywords: `create new add ${spec.title}`, to: `${page.route}?tab=${tab}&action=${encodeURIComponent(make.label)}` });
            }
        }
    }
    items.push(
        { id: 'act:new-flow', kind: 'action', label: t('palette.newFlow'), keywords: 'create template orchestration', to: '/flows?new=1',
            boost: boost.has('/flows') ? 40 : 0 },
        { id: 'act:run-flow', kind: 'action', label: t('palette.runFlow'), keywords: 'start trigger', to: '/flows' },
        { id: 'act:ask', kind: 'action', label: t('palette.askAssistant'), keywords: 'question chat ai', to: '/assistant' },
        // Places that became a tab or a view of another page (L149): still found by their old names.
        ...(nav.some((n) => n.to === '/flows') ? [{ id: 'tab:/flows:sql-workflows', kind: 'tab' as const, label: 'Flows › SQL workflows',
            keywords: 'sql workflow scheduled queries steps', to: '/flows?tab=sql-workflows' }] : []),
        ...(nav.some((n) => n.to === '/products') ? [{ id: 'tab:/products:browse', kind: 'tab' as const, label: 'Data products › Browse',
            keywords: 'marketplace listings request access subscribe', to: '/products?view=browse' }] : []),
        // People & roles' administrator actions, only for those offered Access policies (MANAGE_POLICIES).
        ...(nav.some((n) => n.to === '/governance') ? [
            { id: 'act:new-person', kind: 'action' as const, label: t('palette.newPerson'), detail: t('nav.people'),
                keywords: 'create add user member account directory', to: '/people?tab=people&new=1' },
            { id: 'act:check-access', kind: 'action' as const, label: t('palette.checkAccess'), detail: t('nav.people'),
                keywords: 'why permission can allowed denied effective access user role', to: '/people?tab=access' },
        ] : []),
        // Observability's actions, only where the page is offered (operators).
        ...(nav.some((n) => n.to === '/observability') ? [
            { id: 'act:new-slo', kind: 'action' as const, label: t('palette.newSlo'), detail: t('nav.observability'),
                keywords: 'service level objective error budget availability latency reliability', to: '/observability?tab=slos&new=slo' },
            { id: 'act:new-alert-rule', kind: 'action' as const, label: t('palette.newAlertRule'), detail: t('nav.observability'),
                keywords: 'alert alarm promql logql monitor page on-call', to: '/alerts?tab=platform-rules&new=rule' },
            { id: 'act:find-trace', kind: 'action' as const, label: t('palette.findTrace'), detail: t('nav.observability'),
                keywords: 'trace span request id latency slow error opentelemetry', to: '/observability?tab=journey' },
            { id: 'act:ask-sre', kind: 'action' as const, label: t('palette.askSre'), detail: t('nav.observability'),
                keywords: 'sre agent incident investigate diagnose postgres runbook postmortem on-call remediation',
                to: '/incidents' },

            { id: 'act:new-ops-dashboard', kind: 'action' as const, label: t('palette.newOpsDashboard'), detail: t('nav.observability'),
                keywords: 'dashboard grafana promql logql traceql metrics panel monitoring operations sre', to: '/dashboards?new=ops' },
        ] : []),
        { id: 'act:shortcuts', kind: 'action', label: t('palette.shortcuts'), keywords: 'keys keyboard help', shortcut: '?',
            run: openShortcuts },
    );
    for (const r of readRecent()) items.push({ id: `recent:${r.to}`, kind: 'recent', label: r.label, to: r.to });
    return items;
}

/** What the header's search box asks of its results list (the keys it does not handle itself). */
export interface SearchPanelHandle {
    onKey: (e: React.KeyboardEvent) => void;
}

export interface SearchPanelProps {
    nav: PaletteNavEntry[];
    /** What is typed in the header's box. */
    q: string;
    setQ: (q: string) => void;
    /** An entry was chosen (the box closes and clears). */
    onChosen: () => void;
    /** The highlighted option's id, for the box's aria-activedescendant (undefined: none yet). */
    onActive: (id: string | undefined) => void;
    /** How many options are shown (the box is expanded only when there are some). */
    onCount: (n: number) => void;
}

export const SEARCH_LIST_ID = 'cairn-search-list';

/**
 * The one search's results (L150), under the header's box: what is typed searches the catalog (as the person: Cairn
 * Core answers only what they may see) and also finds pages, tabs and actions (the palette's commands), with "Search all
 * data" and "Ask" at the end. Ctrl K or / focuses the box from anywhere; the arrow keys move and Enter opens.
 */
export const SearchPanel = React.forwardRef<SearchPanelHandle, SearchPanelProps>(function SearchPanel(
    { nav, q, setQ, onChosen, onActive, onCount }, ref) {
    const g = useGuide();
    const t = useT();
    const history = useHistory();
    const session = useDheSession();
    const { client } = useConsumerApi();
    // The highlighted entry, by its id: results re-rank as the person's capabilities and the catalog's answers
    // arrive, and Enter must still choose the entry they moved to, not whatever now sits at its position.
    const [activeId, setActiveId] = useState<string | null>(null);
    const [catalog, setCatalog] = useState<PaletteItem[]>([]);
    const [searching, setSearching] = useState(false);
    const [flows, setFlows] = useState<PaletteItem[] | null>(null);
    const debounced = useDebounced(q, 250);
    const known = (k: string) => !!RESOURCE_SPECS[k];
    const base = useMemo(() => paletteItems(nav, t,
        (route) => visibleTabs(HUBS.find((h) => h.route === route)!, g.caps, known), g.ui.onboarding.role,
        () => g.setShortcuts(true)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [nav, t, g.caps, g.ui.onboarding.role]);

    // The catalog, searched as the person, as they pause typing.
    useEffect(() => {
        const text = debounced.trim();
        if (text.length < 2 || flows) {
            setCatalog([]);
            return undefined;
        }
        const ctl = new AbortController();
        setSearching(true);
        client.search({ query: text, filters: { _entityType: ['DATASET', 'GLOSSARY_TERM', 'DASHBOARD'] }, count: 6 }, ctl.signal)
            .then((r) => setCatalog(r.results.map((e) => ({ id: `cat:${e.urn}`, kind: 'catalog' as const, label: e.name,
                detail: [e.type.toLowerCase().replace(/_/g, ' '), e.platform].filter(Boolean).join(' · '),
                keywords: e.description ?? '', to: assetPath(e.urn) }))))
            .catch(() => setCatalog([]))
            .finally(() => setSearching(false));
        return () => ctl.abort();
    }, [debounced, client, flows]);

    const results = useMemo(() => {
        if (flows) return q.trim() ? rank(flows, q) : flows;
        const text = q.trim();
        const ranked = text ? rank(base, text) : base.filter((i) => i.kind === 'recent' || (i.kind === 'page' && i.boost) || i.id === 'act:ask')
            .slice(0, 12);
        const cat = text ? rank(catalog, text, 6).concat(catalog.filter((c) => !rank([c], text).length)).slice(0, 6) : [];
        const out = [...ranked.slice(0, 30), ...cat];
        if (text) {
            out.push({ id: 'search', kind: 'search', label: t('search.all', { q: text }), to: `/catalog?q=${encodeURIComponent(text)}` });
        }
        if (text && session.feature('assistant', true)) {
            out.push({ id: 'ask', kind: 'ask', label: t('palette.ask', { q: text }), to: `/assistant?q=${encodeURIComponent(text)}` });
        }
        return out;
    }, [base, catalog, q, flows, t, session]);

    useEffect(() => setActiveId(null), [q, flows]);
    const found = activeId === null ? -1 : results.findIndex((r) => r.id === activeId);
    // The first entry is highlighted until the person moves (Enter opens it), as a combobox's list does.
    const active = results.length ? Math.max(0, found) : -1;
    const optionId = (i: number) => `cairn-search-option-${i}`;
    // The box's aria-activedescendant moves in the same update as the highlight (a key press is answered at once).
    const setActive = (i: number) => {
        setActiveId(results[i]?.id ?? null);
        onActive(results[i] ? optionId(i) : undefined);
    };
    useEffect(() => onActive(active >= 0 ? optionId(active) : undefined), [active, onActive]);
    useEffect(() => onCount(results.length), [results.length, onCount]);

    const choose = async (item: PaletteItem | undefined) => {
        if (!item) return;
        if (item.id === 'act:run-flow') {
            setQ('');
            try {
                const list = await client.request<{ id: string; name: string; description?: string }[]>('GET', '/v1/flows');
                setFlows(list.map((f) => ({ id: `flow:${f.id}`, kind: 'action', label: f.name, detail: f.description,
                    to: `/flows?flow=${encodeURIComponent(f.name)}` })));
            } catch {
                setFlows([]);
            }
            return;
        }
        onChosen();
        if (item.run) {
            item.run();
            return;
        }
        if (item.to) {
            if (item.kind === 'page' || item.kind === 'tab') pushRecent(item.to, item.label);
            history.push(item.to);
        }
    };

    useImperativeHandle(ref, () => ({
        onKey: (e: React.KeyboardEvent) => {
            const next = moveActive(e.key, active, results.length);
            if (next !== null) {
                e.preventDefault();
                setActive(next);
                document.getElementById(optionId(next))?.scrollIntoView?.({ block: 'nearest' });
            } else if (e.key === 'Enter') {
                e.preventDefault();
                void choose(results[active]);
            } else if (e.key === 'Backspace' && !q && flows) {
                setFlows(null);
            }
        },
    }));

    return (
        <Panel className="cairn-search-panel">
            <div style={SR_ONLY} aria-live="polite">{t('palette.results', { n: results.length })}</div>
            {results.length > 0 && (
                <List id={SEARCH_LIST_ID} role="listbox" aria-label={t('search.results')}>
                    {/* The pointer highlights an entry only when it moves: a list opening (or scrolling) under a resting
                    pointer must not steal the keyboard's highlight. */}
                {results.map((item, i) => (
                        <li key={item.id} id={optionId(i)} role="option" aria-selected={i === active} data-kind={item.kind}
                            onMouseMove={() => i !== active && setActive(i)} onMouseDown={(e) => e.preventDefault()} onClick={() => void choose(item)}>
                            <span className="label">
                                {item.label}
                                {item.detail && <span className="detail">{item.detail}</span>}
                            </span>
                            {item.shortcut && <kbd>{item.shortcut}</kbd>}
                            <span className="kind">{t(KIND_KEYS[item.kind])}</span>
                        </li>
                    ))}
                </List>
            )}
            {!results.length && <p style={{ margin: '4px', color: tokens.color.textMuted }}>{t('search.hint')}</p>}
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8, color: tokens.color.textMuted, fontSize: 12 }}>
                <span style={{ flex: 1 }}>{t('palette.keys')}</span>
                {searching && <><Spin size="small" /> {t('palette.searching')}</>}
            </div>
        </Panel>
    );
});
