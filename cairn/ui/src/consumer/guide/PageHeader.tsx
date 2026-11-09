import { Tooltip } from 'antd';
import React from 'react';
import { Link, useLocation } from 'react-router-dom';
import styled from 'styled-components';

import { Icon, IconName } from '../components/Icons';
import { focusRing, tokens } from '../components/tokens';
import { HUBS } from '../resources/hubs';
import { useT } from '../i18n';
import { useGuide } from './GuideProvider';
import { hubTab } from './help';

const Head = styled.header`
    display: flex;
    gap: 16px;
    align-items: flex-start;
    flex-wrap: wrap;
    margin-bottom: 20px;
    .titles {
        flex: 1;
        min-width: 240px;
    }
    .title-row {
        display: flex;
        align-items: center;
        gap: 10px;
    }
    h1 {
        margin: 0;
        font-size: 26px;
        font-weight: 700;
    }
    p {
        margin: 4px 0 0;
        color: ${tokens.color.textMuted};
        max-width: 820px;
    }
    .actions {
        display: flex;
        gap: 8px;
        align-items: center;
        flex-wrap: wrap;
    }
`;

const IconBadge = styled.span`
    display: inline-grid;
    place-items: center;
    width: 44px;
    height: 44px;
    border-radius: 12px;
    background: ${tokens.color.primarySoft};
    color: ${tokens.color.primary};
    flex: none;
`;

const Round = styled.button`
    display: inline-grid;
    place-items: center;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    border: 1px solid ${tokens.color.border};
    background: ${tokens.color.surface};
    color: ${tokens.color.textMuted};
    font-weight: 700;
    font-size: 14px;
    cursor: pointer;
    flex: none;
    ${focusRing}
    &:hover {
        color: ${tokens.color.primary};
        border-color: ${tokens.color.primary};
    }
`;

/** The "?" that opens the help panel for what is on screen. */
export function HelpButton({ label }: { label?: string }) {
    const g = useGuide();
    const t = useT();
    const name = label ?? t('help.button');
    return (
        <Tooltip title={name}>
            <Round type="button" aria-label={name} aria-haspopup="dialog" onClick={() => g.setHelp(true)} data-help-button="">
                ?
            </Round>
        </Tooltip>
    );
}

export interface PageHeaderProps {
    title: React.ReactNode;
    description?: React.ReactNode;
    icon?: IconName;
    /** The page's primary action(s). */
    actions?: React.ReactNode;
    children?: React.ReactNode;
}

/** The same header on every page: title, a short description, the primary action, and help. */
export function PageHeader({ title, description, icon, actions, children }: PageHeaderProps) {
    return (
        <Head>
            {icon && <IconBadge><Icon name={icon} size={22} /></IconBadge>}
            <div className="titles">
                <div className="title-row"><h1>{title}</h1><HelpButton /></div>
                {description && <p>{description}</p>}
                {children}
            </div>
            {actions && <div className="actions">{actions}</div>}
        </Head>
    );
}

const Crumbs = styled.nav`
    max-width: 1280px;
    margin: 0 auto;
    padding: 12px 24px 0;
    font-size: 13px;
    color: ${tokens.color.textMuted};
    ol {
        list-style: none;
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin: 0;
        padding: 0;
    }
    li + li::before {
        content: '›';
        margin-right: 6px;
        color: ${tokens.color.textMuted};
    }
    a {
        color: ${tokens.color.primary};
    }
    @media (max-width: 600px) {
        padding: 8px 16px 0;
    }
`;

export interface Crumb {
    label: string;
    to?: string;
}

/** Where a deep page sits: a hub's tab, an asset, a data product, an ontology term. Null for top-level pages. */
export function crumbsFor(pathname: string, search: string, labels: { search: string; marketplace: string; ontology: string },
    tabTitle: (key: string) => string | undefined): Crumb[] | null {
    const hub = HUBS.find((h) => h.route === pathname);
    if (hub) {
        const tab = new URLSearchParams(search).get('tab') ? hubTab(hub, search) : undefined;
        const title = tab ? tabTitle(tab) : undefined;
        return title ? [{ label: hub.title, to: hub.route }, { label: title }] : null;
    }
    const decode = (s: string) => {
        try {
            return decodeURIComponent(s);
        } catch {
            return s;
        }
    };
    let m = /^\/asset\/(.+)$/.exec(pathname);
    if (m) {
        const urn = decode(m[1]);
        const name = /^urn:li:dataset:\([^,]+,(.+),[^,]+\)$/.exec(urn)?.[1] ?? urn.split(':').pop() ?? urn;
        return [{ label: labels.search, to: '/search' }, { label: name }];
    }
    m = /^\/marketplace\/(.+)$/.exec(pathname);
    if (m) return [{ label: labels.marketplace, to: '/marketplace' }, { label: decode(m[1]).split(':').pop() ?? m[1] }];
    m = /^\/ontology\/terms\/(.+)$/.exec(pathname);
    if (m) return [{ label: labels.ontology, to: '/ontology' }, { label: decode(m[1]) }];
    return null;
}

/** Breadcrumbs for the page on screen (none on top-level pages). */
export function Breadcrumbs({ tabTitle }: { tabTitle: (key: string) => string | undefined }) {
    const t = useT();
    const location = useLocation();
    const crumbs = crumbsFor(location.pathname, location.search,
        { search: t('nav.search'), marketplace: t('nav.marketplace'), ontology: t('nav.ontology') }, tabTitle);
    if (!crumbs) return null;
    return (
        <Crumbs aria-label={t('nav.breadcrumb')}>
            <ol>
                {crumbs.map((c, i) => (
                    <li key={`${i}:${c.label}`}>
                        {c.to && i < crumbs.length - 1 ? <Link to={c.to}>{c.label}</Link> : <span aria-current="page">{c.label}</span>}
                    </li>
                ))}
            </ol>
        </Crumbs>
    );
}
