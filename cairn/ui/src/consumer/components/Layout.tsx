import { Badge, Button } from 'antd';
import React from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import styled from 'styled-components';

import { useDheQuery } from '../api/hooks';
import type { InboxItem } from '../api/types';
import { useDheSession } from '../context/CairnProvider';
import { isHosted, navigateHost } from '../hostNav';
import { useHotkeys } from '../utils/useHotkeys';
import { AssistantSlideOver } from './Assistant';
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

export const NAV = [
    { to: '/discover', label: 'Discover' },
    { to: '/search', label: 'Search' },
    { to: '/marketplace', label: 'Marketplace' },
    { to: '/requests', label: 'Requests', badge: true },
    { to: '/context', label: 'Context', feature: 'context' },
    { to: '/governance', label: 'Governance', feature: 'governance' },
    { to: '/reports', label: 'Reports', feature: 'governance' },
    { to: '/settings', label: 'Settings' },
];

export function ConsumerLayout({ children }: { children?: React.ReactNode }) {
    const session = useDheSession();
    const inbox = useDheQuery<InboxItem[]>('/notify/inbox?unread=true', (c, signal) => c.inbox(true, { signal }), {
        pollMs: 30_000,
    });
    useHotkeys({ 'mod+j': () => session.setAssistantOpen(!session.assistantOpen) });
    const unread = inbox.data?.length ?? 0;
    useLocation(); // re-render on navigation (hosted mode reads window.location)
    const hosted = typeof window !== 'undefined' && isHosted(window.location.pathname);
    // In the catalog shell, sections are shell navigation entries: switching moves the shell along.
    const onTab = (to: string) => (e: React.MouseEvent) => {
        if (hosted && navigateHost(to)) e.preventDefault();
    };
    return (
        <div style={{ minHeight: '100%', background: tokens.color.surfaceAlt }}>
            <Bar>
                <strong style={{ marginRight: 16, color: tokens.color.primary }}>Workspace</strong>
                <nav aria-label="Workspace sections" style={{ display: 'flex', gap: 4, flex: 1 }}>
                    {NAV.filter((n) => !n.feature || session.feature(n.feature)).map((n) => (
                        <Tab key={n.to} to={n.to} onClick={onTab(n.to)}>
                            {n.badge ? (
                                <Badge count={unread} size="small" offset={[8, -2]}>
                                    {n.label}
                                </Badge>
                            ) : (
                                n.label
                            )}
                        </Tab>
                    ))}
                </nav>
                <Button
                    type="primary"
                    ghost
                    onClick={() => session.setAssistantOpen(true)}
                    aria-keyshortcuts="Control+J Meta+J"
                    title="Open assistant (⌘J)"
                >
                    ✨ Ask
                </Button>
            </Bar>
            <LicenceBanner />
            <main>{children}</main>
            <AssistantSlideOver />
        </div>
    );
}
