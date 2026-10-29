import { Badge, Button, Select } from 'antd';
import React from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import styled from 'styled-components';

import { useDheQuery } from '../api/hooks';
import type { InboxItem } from '../api/types';
import { useDheSession } from '../context/CairnProvider';
import { isHosted, navigateHost } from '../hostNav';
import { LOCALES, Locale, MessageKey, useI18n, useT } from '../i18n';
import { useHotkeys } from '../utils/useHotkeys';
import { AssistantSlideOver } from './Assistant';
import { useExtensionSections } from './Extensions';
import { LicenceBanner } from './Licence';
import { focusRing, tokens } from './tokens';

const Bar = styled.header`
    position: sticky;
    top: 0;
    z-index: 10;
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 0 24px;
    height: 52px;
    background: ${tokens.color.surface};
    border-bottom: 1px solid ${tokens.color.border};
    overflow-x: auto;
`;

const Tab = styled(NavLink)`
    padding: 6px 12px;
    border-radius: ${tokens.radius.sm};
    color: ${tokens.color.textMuted};
    font-weight: 500;
    white-space: nowrap;
    ${focusRing}
    &.active {
        color: ${tokens.color.primary};
        background: ${tokens.color.primarySoft};
    }
    &:hover {
        color: ${tokens.color.primary};
    }
`;

export const NAV: { to: string; label: MessageKey; badge?: boolean; feature?: string }[] = [
    { to: '/discover', label: 'nav.discover' },
    { to: '/search', label: 'nav.search' },
    { to: '/marketplace', label: 'nav.marketplace' },
    { to: '/requests', label: 'nav.requests', badge: true },
    { to: '/context', label: 'nav.context', feature: 'context' },
    { to: '/governance', label: 'nav.governance', feature: 'governance' },
    { to: '/reports', label: 'nav.reports', feature: 'governance' },
    { to: '/settings', label: 'nav.settings' },
];

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

export function ConsumerLayout({ children }: { children?: React.ReactNode }) {
    const session = useDheSession();
    const t = useT();
    const inbox = useDheQuery<InboxItem[]>('/notify/inbox?unread=true', (c, signal) => c.inbox(true, { signal }), {
        pollMs: 30_000,
    });
    useHotkeys({ 'mod+j': () => session.setAssistantOpen(!session.assistantOpen) });
    const unread = inbox.data?.length ?? 0;
    const hasExtensions = useExtensionSections('workspace').length > 0;
    const nav = hasExtensions
        ? [...NAV.slice(0, -1), { to: '/extensions', label: 'nav.extensions' as MessageKey }, NAV[NAV.length - 1]]
        : NAV;
    useLocation(); // re-render on navigation (hosted mode reads window.location)
    const hosted = typeof window !== 'undefined' && isHosted(window.location.pathname);
    // In the catalog shell, sections are shell navigation entries: switching moves the shell along.
    const onTab = (to: string) => (e: React.MouseEvent) => {
        if (hosted && navigateHost(to)) e.preventDefault();
    };
    return (
        <div style={{ minHeight: '100%', background: tokens.color.surfaceAlt }}>
            <Bar>
                <strong style={{ marginRight: 16, color: tokens.color.primary }}>{t('nav.workspace')}</strong>
                <nav aria-label={t('nav.sections')} style={{ display: 'flex', gap: 4, flex: 1 }}>
                    {nav.filter((n) => !('feature' in n && n.feature) || session.feature(n.feature)).map((n) => (
                        <Tab key={n.to} to={n.to} onClick={onTab(n.to)}>
                            {n.badge ? (
                                <Badge count={unread} size="small" offset={[8, -2]}>
                                    {t(n.label)}
                                </Badge>
                            ) : (
                                t(n.label)
                            )}
                        </Tab>
                    ))}
                </nav>
                <Button
                    type="primary"
                    ghost
                    onClick={() => session.setAssistantOpen(true)}
                    aria-keyshortcuts="Control+J Meta+J"
                    title={t('nav.askTitle')}
                >
                    {t('nav.ask')}
                </Button>
                <LanguagePicker />
            </Bar>
            <LicenceBanner />
            <main>{children}</main>
            <AssistantSlideOver />
        </div>
    );
}
