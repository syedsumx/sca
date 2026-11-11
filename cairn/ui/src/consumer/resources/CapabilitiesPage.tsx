import { Input, Radio } from 'antd';
import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import styled from 'styled-components';

import { Icon } from '../components/Icons';
import { CairnMark, NAV } from '../components/Layout';
import { Page, tokens } from '../components/tokens';
import { MessageKey, useT } from '../i18n';
import data from './capabilities.json';
import { HUBS } from './catalog';
import { HubIcon } from './HubPage';

export interface CapabilityItem { id: string; title: string; summary: string; done: string; status: string; note: string; new?: boolean }
export interface CapabilityLayer { layer: string; compare: string; cairn: string; status: string; note: string }

const STATUS_KEY: Record<string, MessageKey> = { shipped: 'status.shipped', partial: 'status.partial', planned: 'status.planned' };

const H2 = styled.h2`
    margin: 0;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: ${tokens.color.textMuted};
`;

const Chip = styled.span`
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    font-weight: 600;
    padding: 8px 12px;
    border-radius: 999px;
    background: ${tokens.color.surface};
    border: 1px solid ${tokens.color.border};
`;

const NewTag = styled.span`
    font-size: 11px;
    font-weight: 700;
    color: #fff;
    background: ${tokens.color.primary};
    border-radius: 4px;
    padding: 1px 5px;
    white-space: nowrap;
`;

const PartlyTag = styled.span`
    font-size: 11px;
    font-weight: 600;
    color: ${tokens.status.warn.fg};
    background: ${tokens.status.warn.bg};
    border-radius: 4px;
    padding: 1px 5px;
    white-space: nowrap;
`;

const Dot = styled.span<{ $status: string }>`
    flex: none;
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    box-sizing: border-box;
    border: 2px ${(p) => (p.$status === 'planned' ? 'dashed' : 'solid')}
        ${(p) => (p.$status === 'shipped' ? tokens.status.ok.mark : p.$status === 'partial' ? tokens.status.warn.mark : tokens.color.waiting)};
    background: ${(p) => (p.$status === 'shipped' ? tokens.status.ok.mark : 'transparent')};
`;

/** A roadmap status as a mark: a filled circle when shipped and tested, a ring when partly validated (with its words for
 * screen readers; the "partly" tag beside it says it in text too). */
export function StatusMark({ status }: { status: string }) {
    const t = useT();
    return (
        <>
            <Dot $status={status} aria-hidden="true" data-status={status} />
            <span className="sr-only">{t(STATUS_KEY[status] ?? 'status.planned')}</span>
        </>
    );
}

const Section = styled.section`
    background: ${tokens.color.surface};
    border: 1px solid ${tokens.color.border};
    border-radius: 16px;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 16px;
`;

const Steps = styled.ol`
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    li {
        background: ${tokens.color.surfaceSunken};
        border: 1px solid ${tokens.color.border};
        border-radius: 12px;
        padding: 14px 16px;
        display: flex;
        flex-direction: column;
        gap: 6px;
        min-height: 108px;
    }
    .n {
        font-size: 12px;
        font-weight: 600;
        color: ${tokens.color.primary};
    }
    strong {
        font-size: 15px;
        font-weight: 600;
    }
    .b {
        font-size: 13px;
        line-height: 1.45;
        color: ${tokens.color.textMuted};
    }
`;

const Band = styled.div`
    border-radius: 12px;
    background: #0F172A;
    color: #F8FAFC;
    padding: 14px 18px;
    display: flex;
    flex-wrap: wrap;
    gap: 10px 24px;
    align-items: center;
    font-size: 14px;
    strong {
        font-weight: 600;
    }
    span {
        color: #CBD5E1;
    }
`;

const Grid = styled.div<{ $min?: number }>`
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(${(p) => p.$min ?? 260}px, 1fr));
    gap: 16px;
`;

const Card = styled.article`
    overflow-wrap: anywhere;
    background: ${tokens.color.surface};
    border: 1px solid ${tokens.color.border};
    border-radius: ${tokens.radius.lg};
    padding: 18px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
    h3 {
        margin: 0;
        font-family: ${tokens.fontHeading};
        font-size: 18px;
        display: flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
    }
    h3.small {
        font-family: inherit;
        font-size: 15px;
    }
    p {
        margin: 0;
        font-size: 13.5px;
        line-height: 1.5;
        color: ${tokens.color.textMuted};
    }
    p.clamp {
        display: -webkit-box;
        -webkit-line-clamp: 4;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }
    .instead {
        font-size: 12px;
        color: ${tokens.color.textMuted};
    }
`;

const Row = styled.article`
    overflow-wrap: anywhere;
    display: grid;
    grid-template-columns: 56px minmax(0, 1fr);
    gap: 14px;
    padding: 14px 16px;
    border-bottom: 1px solid #F1F5F9;
    &:last-child {
        border-bottom: 0;
    }
    .id {
        font: 600 12px ${tokens.fontMono};
        color: ${tokens.color.textMuted};
        padding-top: 2px;
    }
    h3 {
        margin: 0;
        font-size: 14px;
        font-weight: 600;
        display: flex;
        gap: 8px;
        align-items: center;
        flex-wrap: wrap;
    }
    p {
        margin: 4px 0 0;
        font-size: 13px;
        color: ${tokens.color.textMuted};
    }
`;

const Areas = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 12px;
    a {
        display: flex;
        gap: 12px;
        align-items: center;
        min-height: ${tokens.control}px;
        padding: 12px 14px;
        background: ${tokens.color.surface};
        border: 1px solid ${tokens.color.border};
        border-radius: ${tokens.radius.md};
        color: ${tokens.color.text};
    }
    a:hover {
        border-color: ${tokens.color.primary};
    }
`;

const Hub = styled.div`
    display: grid;
    grid-template-columns: 1fr minmax(220px, 1.1fr) 1fr;
    grid-template-areas: '. catalog .' 'govern core activate' '. extend .';
    gap: 16px;
    align-items: stretch;
    position: relative;
    .core {
        grid-area: core;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        text-align: center;
        gap: 8px;
        padding: 24px;
        border-radius: 50%;
        aspect-ratio: 1;
        background: ${tokens.color.primarySoft};
        border: 3px solid ${tokens.color.primary};
        color: ${tokens.color.text};
        h3 {
            margin: 0;
            font-family: ${tokens.fontHeading};
            font-size: 20px;
            display: flex;
            gap: 8px;
            align-items: center;
            color: ${tokens.color.primary};
        }
        p {
            margin: 0;
            font-size: 13px;
            line-height: 1.45;
            color: ${tokens.color.textMuted};
        }
    }
    ul {
        display: contents;
        list-style: none;
    }
    li {
        display: flex;
        flex-direction: column;
        justify-content: center;
        gap: 6px;
        padding: 16px 18px;
        border-radius: ${tokens.radius.lg};
        background: ${tokens.color.surface};
        border: 1px solid ${tokens.color.border};
        strong {
            font-size: 15px;
        }
        span {
            font-size: 13px;
            line-height: 1.45;
            color: ${tokens.color.textMuted};
        }
    }
    li[data-tier='catalog'] {
        grid-area: catalog;
        border-color: ${tokens.color.primary};
    }
    li[data-tier='govern'] {
        grid-area: govern;
    }
    li[data-tier='activate'] {
        grid-area: activate;
    }
    li[data-tier='extend'] {
        grid-area: extend;
    }
    @media (max-width: 760px) {
        grid-template-columns: 1fr;
        grid-template-areas: 'core' 'catalog' 'govern' 'activate' 'extend';
        .core {
            aspect-ratio: auto;
            border-radius: ${tokens.radius.lg};
        }
    }
`;

const TIERS: { key: 'catalog' | 'govern' | 'activate' | 'extend'; to: string }[] = [
    { key: 'catalog', to: '/catalog' },
    { key: 'govern', to: '/governance' },
    { key: 'activate', to: '/quality' },
    { key: 'extend', to: '/lakehouse' },
];

/** Cairn Core drawn at the centre of the platform, the tiers around it (catalog, govern, activate, extend). */
export function CoreMap() {
    const t = useT();
    return (
        <Section aria-labelledby="map-centre" data-core-map="">
            <H2 id="map-centre">{t('map.centre')}</H2>
            <Hub>
                <article className="core" aria-labelledby="map-core">
                    <h3 id="map-core"><CairnMark size={22} />{t('map.core')}</h3>
                    <p>{t('map.core.body')}</p>
                </article>
                <ul>
                    {TIERS.map((tier) => (
                        <li key={tier.key} data-tier={tier.key}>
                            <strong><Link to={tier.to}>{t(`map.tier.${tier.key}` as MessageKey)}</Link></strong>
                            <span>{t(`map.tier.${tier.key}.body` as MessageKey)}</span>
                        </li>
                    ))}
                </ul>
            </Hub>
        </Section>
    );
}

/** Title of a roadmap item with its marks: status, "partly", "new". */
function ItemTitle({ item }: { item: CapabilityItem }) {
    const t = useT();
    return (
        <h3>
            <StatusMark status={item.status} />
            <span>{item.title}</span>
            {item.status === 'partial' && <PartlyTag>{t('map.partly')}</PartlyTag>}
            {item.new && <NewTag>{t('map.new')}</NewTag>}
        </h3>
    );
}

/**
 * The platform map: how data moves through Cairn, the governance across every step, the platform's layers with how far
 * each is proven, what is new in this release, the ways in, and every roadmap item (all from ROADMAP.md).
 */
export function CapabilitiesPage() {
    const t = useT();
    const { layers, items } = data as { layers: CapabilityLayer[]; items: CapabilityItem[] };
    const [filter, setFilter] = useState<string>('all');
    const [q, setQ] = useState('');
    const shown = useMemo(() => {
        const text = q.trim().toLowerCase();
        return items
            .filter((i) => filter === 'all' || (filter === 'new' ? i.new : i.status === filter))
            .filter((i) => !text || `${i.id} ${i.title} ${i.summary} ${i.done} ${i.note}`.toLowerCase().includes(text))
            .slice()
            .reverse();
    }, [items, filter, q]);
    const fresh = items.filter((i) => i.new);
    const count = (s: string) => items.filter((i) => i.status === s).length;
    return (
        <Page style={{ display: 'flex', flexDirection: 'column', gap: 32 }}>
            <header style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-end', justifyContent: 'space-between', gap: 24 }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10, maxWidth: 760 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: tokens.color.primary }}>
                        <CairnMark size={28} />
                        <span style={{ fontFamily: tokens.fontHeading, fontWeight: 700, fontSize: 20, color: tokens.color.text }}>
                            {t('nav.capabilities')}
                        </span>
                    </div>
                    <h1 style={{ margin: 0, fontSize: 'clamp(28px, 4vw, 44px)', lineHeight: 1.05 }}>{t('map.headline')}</h1>
                    <p style={{ margin: 0, fontSize: 17, lineHeight: 1.55, color: tokens.color.textMuted }}>{t('map.intro')}</p>
                </div>
                <ul aria-label={t('map.legend')} style={{ display: 'flex', flexWrap: 'wrap', gap: 8, listStyle: 'none', margin: 0, padding: 0 }}>
                    <li><Chip><Dot $status="shipped" aria-hidden="true" />{t('status.shipped')}</Chip></li>
                    <li><Chip><Dot $status="partial" aria-hidden="true" />{t('status.partial')}</Chip></li>
                    <li><Chip><NewTag>{t('map.new')}</NewTag>{t('map.legend.new')}</Chip></li>
                </ul>
            </header>

            <CoreMap />

            <Section aria-labelledby="map-flow">
                <H2 id="map-flow">{t('map.flow')}</H2>
                <Steps>
                    {[1, 2, 3, 4, 5].map((n) => (
                        <li key={n}>
                            <span className="n" aria-hidden="true">{String(n).padStart(2, '0')}</span>
                            <strong>{t(`map.flow.${n}` as MessageKey)}</strong>
                            <span className="b">{t(`map.flow.${n}.body` as MessageKey)}</span>
                        </li>
                    ))}
                </Steps>
                <Band>
                    <strong>{t('map.governance')}</strong>
                    <span>{t('map.governance.body')}</span>
                </Band>
            </Section>

            <section aria-labelledby="map-layers" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                <H2 id="map-layers">{t('map.layers')}</H2>
                <Grid>
                    {layers.map((l) => (
                        <Card key={l.layer} data-layer={l.layer}>
                            <h3><StatusMark status={l.status} />{l.layer}
                                {l.status === 'partial' && <PartlyTag>{t('map.partly')}</PartlyTag>}</h3>
                            <span className="instead">{t('map.instead', { what: l.compare })}</span>
                            <p className="clamp" title={l.cairn}>{l.cairn}</p>
                        </Card>
                    ))}
                </Grid>
            </section>

            <section aria-labelledby="map-release" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                <H2 id="map-release">{t('map.release')}</H2>
                {fresh.length === 0 ? <p style={{ margin: 0 }}>{t('map.releaseNone')}</p> : (
                    <Grid $min={300}>
                        {fresh.slice().reverse().map((i) => (
                            <Card key={i.id} data-item={i.id}>
                                <span className="instead" style={{ fontFamily: tokens.fontMono }}>{i.id}</span>
                                <ItemTitle item={i} />
                                <p className="clamp">{i.summary || i.done}</p>
                            </Card>
                        ))}
                    </Grid>
                )}
            </section>

            <section aria-labelledby="map-run" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                <h2 id="map-run" className="sr-only">{t('map.waysIn')}</h2>
                <Grid>
                    {(['waysIn', 'asCode', 'runs'] as const).map((k) => (
                        <Card key={k}>
                            <h3 className="small">{t(`map.${k}` as MessageKey)}</h3>
                            <p>{t(`map.${k}.body` as MessageKey)}</p>
                        </Card>
                    ))}
                </Grid>
            </section>

            <section aria-labelledby="map-areas" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                <H2 id="map-areas">{t('map.areas')}</H2>
                <Areas>
                    {NAV.filter((n) => n.to !== '/capabilities').map((n) => {
                        const hub = HUBS.find((h) => h.route === n.to);
                        return (
                            <Link key={n.to} to={n.to}>
                                <HubIcon style={{ width: 36, height: 36, borderRadius: 10 }}><Icon name={n.icon} /></HubIcon>
                                <span>
                                    <strong style={{ display: 'block', fontWeight: 600 }}>{t(n.label)}</strong>
                                    {hub && <span style={{ fontSize: 12, color: tokens.color.textMuted }}>
                                        {hub.tabs.length} {hub.tabs.length === 1 ? 'area' : 'areas'}</span>}
                                </span>
                            </Link>
                        );
                    })}
                </Areas>
            </section>

            <section aria-labelledby="map-items" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
                    <H2 id="map-items" style={{ flex: 1 }}>{t('map.items')}</H2>
                    <Input.Search allowClear placeholder={t('map.filter')} aria-label={t('map.filter')} value={q}
                        onChange={(e) => setQ(e.target.value)} style={{ width: 260, maxWidth: '100%' }} />
                    <Radio.Group value={filter} onChange={(e) => setFilter(e.target.value)} optionType="button" buttonStyle="solid"
                        style={{ display: 'flex', flexWrap: 'wrap', rowGap: 6 }}
                        aria-label={t('map.show')}
                        options={[{ label: t('map.all', { n: items.length }), value: 'all' },
                            { label: `${t('map.new')} ${fresh.length}`, value: 'new' },
                            { label: `${t('status.shipped')} ${count('shipped')}`, value: 'shipped' },
                            { label: `${t('status.partial')} ${count('partial')}`, value: 'partial' }]} />
                </div>
                <div style={{ background: tokens.color.surface, border: `1px solid ${tokens.color.border}`, borderRadius: tokens.radius.lg }}>
                    {shown.map((i) => (
                        <Row key={i.id} data-item={i.id}>
                            <span className="id">{i.id}</span>
                            <div>
                                <ItemTitle item={i} />
                                <p>{i.done}</p>
                                {i.status !== 'shipped' && i.note && <p style={{ color: tokens.status.warn.fg }}>{i.note}</p>}
                            </div>
                        </Row>
                    ))}
                    {shown.length === 0 && <p style={{ padding: 24, margin: 0, color: tokens.color.textMuted }}>{t('map.noItem')}</p>}
                </div>
            </section>
        </Page>
    );
}
