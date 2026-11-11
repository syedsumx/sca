import { Badge, Input, InputRef, Select, Tooltip } from 'antd';
import React, { Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { Link, NavLink, useHistory, useLocation } from 'react-router-dom';
import styled, { css } from 'styled-components';

import { useDheQuery } from '../api/hooks';
import type { InboxItem } from '../api/types';
import { useDheSession } from '../context/CairnProvider';
import { isHosted, navigateHost } from '../hostNav';
import { LOCALES, Locale, MessageKey, useI18n, useT } from '../i18n';
import { useHotkeys } from '../utils/useHotkeys';
import { routeAllowed } from '../guide/capabilities';
import { GuideProvider, useGuide } from '../guide/GuideProvider';
import { arrangeNav } from '../guide/nav';
import { RoleDialog } from '../guide/GetStarted';
import { Breadcrumbs, HelpButton } from '../guide/PageHeader';
import { pushRecent } from '../guide/palette';
import { ShortcutsSheet, SidebarCustomiser } from '../guide/Sheets';
import { HUBS } from '../resources/hubs';
import { AssistantSlideOver } from './Assistant';
import { useExtensionSections } from './Extensions';
import { Icon, IconName } from './Icons';
import { LicenceBanner } from './Licence';
import { ConsumerTheme, ThemeVariables, focusRing, tokens } from './tokens';

export interface NavItem {
    to: string;
    label: MessageKey;
    icon: IconName;
    badge?: boolean;
    feature?: string;
    exact?: boolean;
}

/**
 * The workspace's sections, grouped as they appear in the sidebar (L149: one home per concept). Cairn Core (the
 * catalog: assets, data products, glossary, domains, semantics and lineage) comes first; every other group is built
 * around it: governing it, keeping it healthy, analysing it, building what feeds it, AI on top of it, and the
 * platform itself. Search (the palette), Ask, Inbox and Profile are in the header. Every earlier route still opens:
 * it redirects to its new home (moves.ts).
 */
export const NAV_GROUPS: { label: MessageKey; items: NavItem[] }[] = [
    {
        label: 'nav.group.catalog',
        items: [
            { to: '/discover', label: 'nav.homeSection', icon: 'home' },
            { to: '/catalog', label: 'nav.searchSection', icon: 'catalog' },
            { to: '/products', label: 'nav.products', icon: 'cube' },
            { to: '/glossary', label: 'nav.glossary', icon: 'tag' },
            { to: '/domains', label: 'nav.domains', icon: 'domain' },
            { to: '/semantics', label: 'nav.semantics', icon: 'layers' },
            { to: '/lineage', label: 'nav.lineage', icon: 'lineage' },
            { to: '/requests', label: 'nav.requests', icon: 'inbox' },
        ],
    },
    {
        label: 'nav.group.govern',
        items: [
            { to: '/governance', label: 'nav.accessPolicies', icon: 'shield', feature: 'governance' },
            { to: '/people', label: 'nav.people', icon: 'user' },
            { to: '/automation', label: 'nav.automation', icon: 'repeat', feature: 'governance' },
            { to: '/security', label: 'nav.security', icon: 'lock' },
            { to: '/sharing', label: 'nav.externalSharing', icon: 'share' },
            { to: '/usage', label: 'nav.usageCost', icon: 'coin' },
            { to: '/reports', label: 'nav.governanceReport', icon: 'report', feature: 'governance' },
        ],
    },
    {
        label: 'nav.group.quality',
        items: [
            { to: '/quality', label: 'nav.quality', icon: 'contract' },
            { to: '/runs', label: 'nav.runs', icon: 'play' },
            { to: '/incidents', label: 'nav.incidents', icon: 'bell' },
            { to: '/observability', label: 'nav.platformHealth', icon: 'pulse' },
            { to: '/alerts', label: 'nav.alerts', icon: 'bell' },
        ],
    },
    {
        label: 'nav.group.analyze',
        items: [
            { to: '/sql', label: 'nav.sql', icon: 'code', exact: true },
            { to: '/sql/workbooks', label: 'nav.workbooks', icon: 'sheet' },
            { to: '/dashboards', label: 'nav.dashboards', icon: 'chart' },
            { to: '/sql/input-tables', label: 'nav.inputTables', icon: 'table' },
        ],
    },
    {
        label: 'nav.group.build',
        items: [
            { to: '/ingestion', label: 'nav.ingestion', icon: 'download' },
            { to: '/lakehouse', label: 'nav.lakehouse', icon: 'database' },
            { to: '/flows', label: 'nav.flows', icon: 'flow' },
        ],
    },
    {
        label: 'nav.group.ai',
        items: [
            { to: '/assistant', label: 'nav.assistant', icon: 'sparkle' },
            { to: '/agents', label: 'nav.agents', icon: 'bot' },
            { to: '/ml', label: 'nav.ml', icon: 'brain' },
            { to: '/ai/gateway', label: 'nav.aiGateway', icon: 'server' },
        ],
    },
    {
        label: 'nav.group.platform',
        items: [
            { to: '/compute', label: 'nav.computeSection', icon: 'server' },
            { to: '/settings', label: 'nav.settings', icon: 'gear' },
            { to: '/capabilities', label: 'nav.platformMap', icon: 'grid' },
        ],
    },
];

/**
 * Pages that are no sidebar entry but have a home elsewhere: the header (Inbox, Profile), the guide and devices. Every
 * route of an earlier layout redirects to its new home instead (moves.ts, MOVED_ROUTES); a test checks each lands on
 * a page, and that none of these is a 404.
 */
export const KEPT_ROUTES = ['/inbox', '/profile', '/get-started', '/extensions', '/device'];

/** Every section, in sidebar order. */
export const NAV: NavItem[] = NAV_GROUPS.flatMap((g) => g.items);

// The command palette and the help panel load on first use (their own chunk: they read the areas' catalog).
const CommandPalette = React.lazy(() => import('../guide/CommandPalette').then((m) => ({ default: m.CommandPalette })));
const HelpPanel = React.lazy(() => import('../guide/HelpPanel').then((m) => ({ default: m.HelpPanel })));
const GET_STARTED: NavItem = { to: '/get-started', label: 'nav.getStarted', icon: 'sparkle' };
const noTabs = () => undefined;

const COLLAPSE_KEY = 'cairn.sidebar.collapsed';

/** The UI language; the choice is kept in this browser. */
export function LanguagePicker() {
    const { locale, setLocale, t } = useI18n();
    return (
        <Select
            size="small"
            bordered={false}
            aria-label={t('nav.language')}
            value={locale}
            onChange={(v) => setLocale(v as Locale)}
            options={LOCALES.map((l) => ({ value: l.code, label: l.label }))}
            style={{ width: 110 }}
        />
    );
}

const Shell = styled.div`
    display: flex;
    min-height: 100%;
    background: ${tokens.color.surfaceAlt};
`;

const Side = styled.aside<{ $collapsed: boolean; $open: boolean }>`
    position: sticky;
    top: 0;
    flex: none;
    height: 100vh;
    width: ${(p) => (p.$collapsed ? '76px' : '248px')};
    background: ${tokens.color.sidebar};
    color: ${tokens.color.sidebarText};
    border-right: 1px solid ${tokens.color.border};
    display: flex;
    flex-direction: column;
    transition: width 0.18s ease;
    z-index: 30;
    @media (max-width: 960px) {
        position: fixed;
        left: 0;
        width: 272px;
        transform: translateX(${(p) => (p.$open ? '0' : '-100%')});
        transition: transform 0.2s ease;
        box-shadow: ${(p) => (p.$open ? '0 0 0 100vmax rgba(15,23,42,.45)' : 'none')};
    }
    @media (prefers-reduced-motion: reduce) {
        transition: none;
    }
`;

/** The Cairn mark: a cairn's outline, in the accent colour. */
export function CairnMark({ size = 22 }: { size?: number }) {
    return (
        <svg width={size} height={size} viewBox="0 0 28 28" aria-hidden="true" focusable="false">
            <path d="M14 3 L24 23 H4 Z" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinejoin="round" />
            <path d="M9 17 H19" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" />
        </svg>
    );
}

const Brand = styled(Link)<{ $collapsed: boolean }>`
    display: flex;
    align-items: center;
    gap: 9px;
    min-height: 60px;
    padding: 0 ${(p) => (p.$collapsed ? '0' : '20px')};
    justify-content: ${(p) => (p.$collapsed ? 'center' : 'flex-start')};
    flex: none;
    && {
        color: ${tokens.color.text};
    }
    font-family: ${tokens.fontHeading};
    font-weight: 700;
    font-size: 17px;
    text-decoration: none;
    letter-spacing: -0.01em;
    svg {
        color: ${tokens.color.primary};
    }
    ${focusRing}
    &&:hover {
        color: ${tokens.color.primaryHover};
    }
`;

const NavScroll = styled.nav`
    flex: 1;
    overflow-y: auto;
    padding: 0 12px 16px;
    scrollbar-width: thin;
    scrollbar-color: ${tokens.color.borderStrong} transparent;
`;

const Group = styled.div<{ $collapsed: boolean }>`
    margin-top: 12px;
    h2 {
        margin: 0 0 2px;
        padding: 0 12px;
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: ${tokens.color.textMuted};
        ${(p) =>
            p.$collapsed &&
            css`
                height: 1px;
                overflow: hidden;
                background: ${tokens.color.border};
                margin: 0 8px 8px;
                padding: 0;
                color: transparent;
            `}
    }
    ul {
        list-style: none;
        margin: 0;
        padding: 0;
    }
`;

const Item = styled(NavLink)<{ $collapsed: boolean }>`
    && {
        color: ${tokens.color.sidebarText};
    }
    &&:hover {
        color: ${tokens.color.text};
    }
    &&.active {
        color: ${tokens.color.sidebarActive};
    }
    .ant-badge {
        color: inherit;
    }
    position: relative;
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: ${tokens.control}px;
    padding: 0 12px;
    border-radius: ${tokens.radius.sm};
    font-size: 14px;
    font-weight: 500;
    text-decoration: none;
    white-space: nowrap;
    overflow: hidden;
    ${(p) => p.$collapsed && 'justify-content: center; padding: 0;'}
    ${focusRing}
    &:hover {
        background: #F1F5F9;
    }
    &.active {
        font-weight: 600;
        background: ${tokens.color.primarySoft};
        box-shadow: inset 3px 0 0 ${tokens.color.primary};
    }
    .label {
        flex: 1;
        overflow: hidden;
        text-overflow: ellipsis;
    }
`;

const SideFoot = styled.div`
    flex: none;
    border-top: 1px solid ${tokens.color.border};
    padding: 8px 10px;
    display: flex;
    gap: 4px;
    align-items: center;
    justify-content: flex-end;
    .more {
        flex: 1;
        min-width: 0;
        min-height: ${tokens.control}px;
        border: 0;
        background: transparent;
        color: ${tokens.color.sidebarText};
        text-align: left;
        padding: 0 10px;
        border-radius: ${tokens.radius.sm};
        cursor: pointer;
        font-size: 13px;
        ${focusRing}
        &:hover {
            color: ${tokens.color.text};
            background: #F1F5F9;
        }
    }
`;

const PaletteButton = styled.button`
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-height: ${tokens.control}px;
    padding: 0 12px;
    border: 1px solid ${tokens.color.borderStrong};
    border-radius: ${tokens.radius.md};
    background: ${tokens.color.surfaceSunken};
    color: ${tokens.color.textMuted};
    font: inherit;
    font-size: 13px;
    cursor: pointer;
    white-space: nowrap;
    ${focusRing}
    &:hover {
        color: ${tokens.color.text};
        border-color: ${tokens.color.primary};
    }
    kbd {
        font: 12px ${tokens.font};
        color: ${tokens.color.textMuted};
        border: 1px solid ${tokens.color.borderStrong};
        border-radius: 5px;
        padding: 0 6px;
        background: ${tokens.color.surface};
    }
    @media (max-width: 720px) {
        .text,
        kbd {
            display: none;
        }
    }
`;

const IconButton = styled.button`
    display: inline-grid;
    place-items: center;
    width: ${tokens.control}px;
    height: ${tokens.control}px;
    border: 0;
    border-radius: ${tokens.radius.sm};
    background: transparent;
    color: inherit;
    cursor: pointer;
    flex: none;
    ${focusRing}
    &:hover {
        background: #F1F5F9;
    }
`;

const Column = styled.div`
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
`;

const Top = styled.header`
    position: sticky;
    top: 0;
    z-index: 20;
    display: flex;
    align-items: center;
    gap: 8px 12px;
    height: 60px;
    padding: 0 32px;
    box-sizing: border-box;
    /* On a phone the bar wraps: the search box gets a row of its own. */
    @media (max-width: 720px) {
        flex-wrap: wrap;
        height: auto;
        min-height: 60px;
        padding: 8px 16px;
    }
    background: ${tokens.color.surface};
    border-bottom: 1px solid ${tokens.color.border};
    .menu {
        display: none;
        color: ${tokens.color.text};
    }
    @media (max-width: 960px) {
        padding-left: 16px;
        padding-right: 16px;
        .menu {
            display: inline-grid;
        }
    }
`;

const SearchBox = styled(Input)`
    max-width: 520px;
    flex: 1 1 240px;
    && {
        min-height: ${tokens.control}px;
        border-radius: ${tokens.radius.md};
        background: ${tokens.color.surface};
        border-color: ${tokens.color.borderStrong};
    }
    &&:hover {
        border-color: ${tokens.color.primary};
    }
    && input {
        background: transparent;
    }
    kbd {
        font: 12px ${tokens.font};
        color: ${tokens.color.textMuted};
        border: 1px solid ${tokens.color.borderStrong};
        border-radius: 5px;
        padding: 0 6px;
        background: ${tokens.color.surface};
    }
    @media (max-width: 720px) {
        order: 10;
        flex-basis: 100%;
        max-width: none;
    }
`;

const AskButton = styled.button`
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-height: ${tokens.control}px;
    padding: 0 16px;
    border: 1px solid ${tokens.color.borderStrong};
    border-radius: ${tokens.radius.md};
    color: ${tokens.color.text};
    background: ${tokens.color.surface};
    font: inherit;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    white-space: nowrap;
    ${focusRing}
    &:hover {
        border-color: ${tokens.color.primary};
        color: ${tokens.color.primaryHover};
    }
`;

const Avatar = styled.span`
    display: inline-grid;
    place-items: center;
    width: 36px;
    height: 36px;
    border-radius: 50%;
    background: ${tokens.color.primary};
    color: #fff;
    font-weight: 600;
    font-size: 13px;
    flex: none;
`;

const InboxLink = styled(Link)`
    display: inline-grid;
    place-items: center;
    width: ${tokens.control}px;
    height: ${tokens.control}px;
    border-radius: ${tokens.radius.sm};
    && {
        color: ${tokens.color.text};
    }
    flex: none;
    ${focusRing}
    &:hover {
        background: #F1F5F9;
    }
    .ant-badge {
        color: inherit;
    }
`;

const ProfileLink = styled(Link)`
    display: inline-grid;
    place-items: center;
    min-width: ${tokens.control}px;
    min-height: ${tokens.control}px;
    border-radius: 50%;
    flex: none;
    ${focusRing}
`;

/** Compact section tabs for when a catalog shell provides the main navigation. */
const HostedBar = styled.nav`
    display: flex;
    gap: 4px;
    padding: 6px 24px;
    overflow-x: auto;
    background: ${tokens.color.surface};
    border-bottom: 1px solid ${tokens.color.border};
    a {
        display: inline-flex;
        align-items: center;
        min-height: ${tokens.control}px;
        padding: 0 12px;
        border-radius: ${tokens.radius.sm};
        color: ${tokens.color.textSubtle};
        font-weight: 500;
        white-space: nowrap;
        text-decoration: none;
        ${focusRing}
    }
    a:hover {
        background: #F1F5F9;
    }
    a.active {
        color: ${tokens.color.primaryHover};
        font-weight: 600;
        background: ${tokens.color.primarySoft};
    }
`;

/** The palette's shortcut as this computer writes it. */
export function paletteKeys(platform: string = typeof navigator !== 'undefined' ? navigator.platform || '' : ''): string {
    return /mac|iphone|ipad/i.test(platform) ? '⌘ K' : 'Ctrl K';
}

function initials(name: string | undefined): string {
    const parts = (name || '').trim().split(/\s+/).filter(Boolean);
    return ((parts[0]?.[0] ?? '') + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase() || '•';
}

function readCollapsed(): boolean {
    try {
        return window.localStorage.getItem(COLLAPSE_KEY) === '1';
    } catch {
        return false;
    }
}

/** The workspace's shell: sidebar, top bar, and the guided experience (onboarding, palette, help, shortcuts). */
export function ConsumerLayout({ children }: { children?: React.ReactNode }) {
    return (
        <GuideProvider>
            <Shell0>{children}</Shell0>
        </GuideProvider>
    );
}

function Shell0({ children }: { children?: React.ReactNode }) {
    const session = useDheSession();
    const guide = useGuide();
    const searchRef = useRef<InputRef>(null);
    const gAt = useRef(0);
    const t = useT();
    const history = useHistory();
    const location = useLocation();
    const inbox = useDheQuery<InboxItem[]>('/notify/inbox?unread=true', (c, signal) => c.inbox(true, { signal }), {
        pollMs: 30_000,
    });
    const [collapsed, setCollapsed] = useState(readCollapsed);
    const [open, setOpen] = useState(false);
    const [q, setQ] = useState('');
    useHotkeys({
        'mod+j': () => session.setAssistantOpen(!session.assistantOpen),
        'mod+k': () => guide.setPalette(true),
        '?': () => guide.setShortcuts(true),
        '/': () => {
            // A page's own search box (Discover, Search) takes it first; elsewhere, the top bar's.
            if (document.activeElement?.tagName !== 'INPUT') searchRef.current?.focus();
        },
        g: () => {
            gAt.current = Date.now();
        },
        h: () => {
            if (Date.now() - gAt.current < 1200) history.push('/get-started');
        },
    });
    useEffect(() => setOpen(false), [location.pathname]);
    // A new page starts at its top (the hash router keeps the old page's scroll otherwise, under the sticky top bar).
    useEffect(() => {
        if (typeof window.scrollTo === 'function' && !/jsdom/i.test(navigator.userAgent)) window.scrollTo(0, 0);
    }, [location.pathname]);
    const unread = inbox.data?.length ?? 0;
    const hasExtensions = useExtensionSections('workspace').length > 0;
    const visible = (n: NavItem) => (!n.feature || session.feature(n.feature)) && routeAllowed(n.to, HUBS, guide.caps);
    const allGroups = NAV_GROUPS.map((g) => ({
        ...g,
        items: [
            ...g.items,
            ...(g.label === 'nav.group.platform' && hasExtensions
                ? [{ to: '/extensions', label: 'nav.extensions' as MessageKey, icon: 'puzzle' as IconName }]
                : []),
        ],
    }));
    // What the person may use (capabilities), then arranged for their role and their own pins and hidden entries.
    const arranged = arrangeNav(allGroups, visible, guide.ui.onboarding.role, guide.ui.sidebar);
    const groups = [
        ...(arranged.forYou.length ? [{ label: 'nav.forYou' as MessageKey, items: arranged.forYou }] : []),
        ...arranged.groups,
    ];
    const usable = allGroups.flatMap((g) => g.items).filter(visible);
    const paletteNav = useMemo(() => [
        ...(guide.enabled ? [GET_STARTED] : []), ...usable,
        // The header's Inbox and Profile are pages too.
        { to: '/inbox', label: 'nav.inbox' as MessageKey, icon: 'inbox' as IconName },
        { to: '/profile', label: 'nav.profile' as MessageKey, icon: 'user' as IconName },
    ].map((n) => ({ to: n.to, label: n.to === '/assistant' ? t('palette.askAssistant') : t(n.label) })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [usable.map((n) => n.to).join(), guide.enabled, t]);
    const toggleEverything = () => void guide.save({ sidebar: { show_all: !guide.ui.sidebar.show_all } });
    const hosted = typeof window !== 'undefined' && isHosted(window.location.pathname);
    // In the catalog shell, sections are shell navigation entries: switching moves the shell along.
    const onNav = (to: string, label?: MessageKey) => (e: React.MouseEvent) => {
        if (label) pushRecent(to, t(label));
        if (hosted && navigateHost(to)) e.preventDefault();
    };
    const toggle = () => {
        const next = !collapsed;
        setCollapsed(next);
        try {
            window.localStorage.setItem(COLLAPSE_KEY, next ? '1' : '0');
        } catch {
            // storage can be blocked; the choice then lasts for this page only
        }
    };
    const name = session.config?.user?.display_name;
    const label = (n: NavItem) => t(n.label);

    const askAndSearch = (
        <>
            <PaletteButton type="button" onClick={() => guide.setPalette(true)} aria-haspopup="dialog"
                aria-keyshortcuts="Control+K Meta+K" aria-label={t('palette.open')}
                title={t('nav.searchShortcut', { keys: paletteKeys() })}>
                <Icon name="search" size={16} />
                <span className="text">{t('palette.open')}</span>
                <kbd>{paletteKeys()}</kbd>
            </PaletteButton>
            <SearchBox
                ref={searchRef}
                aria-label={t('nav.search')}
                placeholder={t('nav.searchPlaceholder')}
                prefix={<Icon name="search" size={16} />}
                suffix={<kbd>/</kbd>}
                value={q}
                onChange={(e) => setQ(e.target.value)}
                onPressEnter={() => {
                    // One results page (L149): the top bar searches the catalog; the palette keeps commands.
                    history.push(`/catalog?q=${encodeURIComponent(q.trim())}`);
                    setQ('');
                }}
            />
            <span style={{ flex: 1 }} />
            <AskButton
                type="button"
                onClick={() => session.setAssistantOpen(true)}
                aria-keyshortcuts="Control+J Meta+J"
                title={t('nav.askTitle')}
            >
                {t('nav.ask')}
            </AskButton>
            <HelpButton />
            <Tooltip title={t('nav.inbox')}>
                <InboxLink to="/inbox" aria-label={unread ? t('nav.inboxCount', { n: unread }) : t('nav.inbox')}
                    onClick={onNav('/inbox', 'nav.inbox')}>
                    <Badge count={unread} size="small" offset={[2, -2]}><Icon name="inbox" size={20} /></Badge>
                </InboxLink>
            </Tooltip>
            <LanguagePicker />
            {name && (
                <Tooltip title={name}>
                    <ProfileLink to="/profile" aria-label={t('nav.yourProfile', { name })} onClick={onNav('/profile', 'nav.profile')}>
                        <Avatar aria-hidden="true">{initials(name)}</Avatar>
                    </ProfileLink>
                </Tooltip>
            )}
        </>
    );

    if (hosted) {
        return (
            <div className="cairn-shell" style={{ minHeight: '100%', background: tokens.color.surfaceAlt }}>
                <ConsumerTheme />
                <ThemeVariables />
                <Top>{askAndSearch}</Top>
                <HostedBar aria-label={t('nav.sections')}>
                    {[...(guide.enabled ? [GET_STARTED] : []), ...usable].map((n) => (
                        <NavLink key={n.to} to={n.to} exact={n.exact} onClick={onNav(n.to, n.label)}>
                            {label(n)}
                        </NavLink>
                    ))}
                </HostedBar>
                <LicenceBanner />
                <Breadcrumbs tabTitle={noTabs} />
                <main>{children}</main>
                <AssistantSlideOver />
                <RoleDialog />
                <ShortcutsSheet />
                <SidebarCustomiser entries={usable.map((n) => ({ to: n.to, label: t(n.label) }))} />
                {guide.palette && <Suspense fallback={null}><CommandPalette nav={paletteNav} /></Suspense>}
                {guide.help && <Suspense fallback={null}><HelpPanel pageTitle={(path) => {
                    const n = usable.find((x) => x.to === path) ?? (path === '/get-started' ? GET_STARTED : undefined);
                    return n ? t(n.label) : undefined;
                }} /></Suspense>}
            </div>
        );
    }

    return (
        <Shell className="cairn-shell">
            <ConsumerTheme />
            <ThemeVariables />
            <Side $collapsed={collapsed} $open={open}>
                <Brand to="/discover" $collapsed={collapsed} aria-label={t('nav.home')} onClick={onNav('/discover')}>
                    <CairnMark />
                    {!collapsed && <span>Cairn</span>}
                </Brand>
                <NavScroll aria-label={t('nav.sections')}>
                    {guide.enabled && (
                        <Group $collapsed={collapsed}>
                            <ul>
                                <li>
                                    <Tooltip title={collapsed ? t(GET_STARTED.label) : ''} placement="right">
                                        <Item to={GET_STARTED.to} $collapsed={collapsed} aria-label={collapsed ? t(GET_STARTED.label) : undefined}>
                                            <Icon name={GET_STARTED.icon} />
                                            {!collapsed && <span className="label">{t(GET_STARTED.label)}</span>}
                                        </Item>
                                    </Tooltip>
                                </li>
                            </ul>
                        </Group>
                    )}
                    {groups.map((g) => (
                        <Group key={g.label} $collapsed={collapsed}>
                            <h2>{t(g.label as MessageKey)}</h2>
                            <ul>
                                {g.items.map((n) => (
                                    <li key={n.to}>
                                        <Tooltip title={collapsed ? t(n.label) : ''} placement="right">
                                            <Item to={n.to} exact={n.exact} $collapsed={collapsed} onClick={onNav(n.to, n.label)}
                                                aria-label={collapsed ? t(n.label) : undefined}>
                                                <Icon name={n.icon} />
                                                {!collapsed && <span className="label">{label(n)}</span>}
                                            </Item>
                                        </Tooltip>
                                    </li>
                                ))}
                            </ul>
                        </Group>
                    ))}
                </NavScroll>
                <SideFoot>
                    {!collapsed && guide.available && (
                        <>
                            <button type="button" className="more" onClick={toggleEverything} aria-pressed={guide.ui.sidebar.show_all}>
                                {guide.ui.sidebar.show_all ? t('nav.showLess')
                                    : `${t('nav.showEverything')}${arranged.folded ? ` ${t('nav.folded', { n: arranged.folded })}` : ''}`}
                            </button>
                            <IconButton type="button" onClick={() => guide.setCustomise(true)} aria-label={t('nav.customise')}
                                title={t('nav.customise')}>
                                <Icon name="gear" size={16} />
                            </IconButton>
                        </>
                    )}
                    <IconButton type="button" onClick={toggle} aria-label={t(collapsed ? 'nav.expand' : 'nav.collapse')}
                        title={t(collapsed ? 'nav.expand' : 'nav.collapse')}>
                        <span style={{ display: 'inline-flex', transform: collapsed ? 'none' : 'rotate(180deg)' }}>
                            <Icon name="chevron" />
                        </span>
                    </IconButton>
                </SideFoot>
            </Side>
            <Column>
                <Top>
                    <IconButton type="button" className="menu" onClick={() => setOpen(!open)} aria-label={t('nav.menu')}
                        aria-expanded={open}>
                        <Icon name="menu" />
                    </IconButton>
                    {askAndSearch}
                </Top>
                <LicenceBanner />
                <Breadcrumbs tabTitle={noTabs} />
                <main style={{ flex: 1 }}>{children}</main>
            </Column>
            {open && <div aria-hidden="true" onClick={() => setOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 25 }} />}
            <AssistantSlideOver />
            <RoleDialog />
            <ShortcutsSheet />
            <SidebarCustomiser entries={usable.map((n) => ({ to: n.to, label: t(n.label) }))} />
            {guide.palette && <Suspense fallback={null}><CommandPalette nav={paletteNav} /></Suspense>}
            {guide.help && <Suspense fallback={null}><HelpPanel pageTitle={(path) => {
                const n = usable.find((x) => x.to === path) ?? (path === '/get-started' ? GET_STARTED : undefined);
                return n ? t(n.label) : undefined;
            }} /></Suspense>}
        </Shell>
    );
}
