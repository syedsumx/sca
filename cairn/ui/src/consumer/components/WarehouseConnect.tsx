/**
 * Snowflake and Databricks tables in place (L151): Lakehouse › External catalogs leads with a card per warehouse
 * preset. Each card opens the area's own generated form for that preset (`?action=<label>`, the external-catalogs and
 * SaaS-sync actions in catalog.ts), so what is sent is what the API, the CLI and Terraform take.
 */
import React from 'react';
import { Link, useLocation } from 'react-router-dom';
import styled from 'styled-components';

import { useT } from '../i18n';
import type { MessageKey } from '../i18n/messages';
import { RESOURCE_SPECS } from '../resources/catalog';
import { ResourceView } from '../resources/ResourceView';
import { Card } from './HomeCards';
import { Muted, tokens } from './tokens';

type Kind = 'inPlace' | 'live' | 'perVersion';

/** The presets, each with the catalog.ts action (its label) that opens its form, and the tab that action is on. */
export const WAREHOUSE_PRESETS: { key: string; kind: Kind; tab: string; action: string }[] = [
    { key: 'openCatalog', kind: 'inPlace', tab: 'external-catalogs', action: 'Connect Snowflake Open Catalog' },
    { key: 'horizon', kind: 'inPlace', tab: 'external-catalogs', action: 'Connect Snowflake (Horizon)' },
    { key: 'unity', kind: 'inPlace', tab: 'external-catalogs', action: 'Connect Databricks Unity Catalog' },
    { key: 'snowflakeLive', kind: 'live', tab: 'external-catalogs', action: 'Connect Snowflake (live)' },
    { key: 'deltaSharing', kind: 'perVersion', tab: 'saas-syncs', action: 'Read a Delta Sharing table' },
];

const HUB = { 'external-catalogs': '/lakehouse', 'saas-syncs': '/ingestion' } as Record<string, string>;

const Grid = styled.ul`
    list-style: none;
    margin: 0 0 ${tokens.space(6)};
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: ${tokens.space(3)};
    li > section {
        height: 100%;
        padding: 16px;
    }
    h3 {
        margin: 0;
        font-size: 15px;
        font-weight: 600;
    }
    p {
        margin: 0;
        flex: 1;
    }
    a.cairn-connect {
        display: inline-flex;
        align-items: center;
        min-height: ${tokens.control}px;
        font-weight: 600;
    }
`;

/** A word for how the data is reached, in the shape and words of a tag (never colour alone). */
const KindTag = styled.span`
    align-self: flex-start;
    font-size: 12px;
    line-height: 20px;
    padding: 0 8px;
    border-radius: 999px;
    border: 1px solid ${tokens.color.borderStrong};
    color: ${tokens.color.textSubtle};
`;

export function WarehouseCards() {
    const t = useT();
    const location = useLocation();
    return (
        <section aria-labelledby="cairn-wh-title">
            <h2 id="cairn-wh-title" style={{ fontSize: 16, fontWeight: 600, margin: '0 0 4px' }}>{t('wh.title')}</h2>
            <p style={{ margin: '0 0 12px' }}><Muted>{t('wh.intro')} {t('wh.secrets')}</Muted></p>
            <Grid aria-label={t('wh.title')}>
                {WAREHOUSE_PRESETS.map((p) => {
                    const title = t(`wh.${p.key}.title` as MessageKey);
                    const here = HUB[p.tab] === location.pathname || p.tab === 'external-catalogs';
                    const to = { pathname: here ? location.pathname : HUB[p.tab],
                                 search: `?tab=${p.tab}&action=${encodeURIComponent(p.action)}` };
                    return (
                        <li key={p.key}>
                            <Card aria-label={title}>
                                <KindTag>{t(`wh.${p.kind}` as MessageKey)}</KindTag>
                                <h3>{title}</h3>
                                <p><Muted>{t(`wh.${p.key}.body` as MessageKey)}</Muted></p>
                                <Link className="cairn-connect" to={to}>{t('wh.connect', { name: title })}</Link>
                            </Card>
                        </li>
                    );
                })}
            </Grid>
        </section>
    );
}

/** The External catalogs tab: the warehouse cards, then the catalogs registered (the generic table and forms). */
export function ExternalCatalogsView() {
    return (
        <>
            <WarehouseCards />
            <ResourceView spec={RESOURCE_SPECS['external-catalogs']} />
        </>
    );
}
