import { Input, Radio, Tag } from 'antd';
import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import styled from 'styled-components';

import { Icon } from '../components/Icons';
import { NAV } from '../components/Layout';
import { Muted, Page, tokens } from '../components/tokens';
import { useT } from '../i18n';
import data from './capabilities.json';
import { HUBS } from './catalog';
import { HubIcon } from './HubPage';

interface Item { id: string; title: string; summary: string; done: string; status: string; note: string }
interface Layer { layer: string; compare: string; cairn: string; status: string; note: string }

const STATUS: Record<string, { color: string; label: string }> = {
    shipped: { color: 'green', label: 'Shipped and tested' },
    partial: { color: 'gold', label: 'Partly validated' },
    planned: { color: 'default', label: 'Planned' },
};

const Stats = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 12px;
    margin: 4px 0 24px;
    div {
        background: ${tokens.color.surface};
        border: 1px solid ${tokens.color.border};
        border-radius: ${tokens.radius.md};
        padding: 14px 16px;
        box-shadow: ${tokens.shadow.card};
    }
    strong {
        display: block;
        font-size: 26px;
        font-weight: 700;
        letter-spacing: -0.02em;
    }
`;

const Layers = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 12px;
    margin-bottom: 28px;
    article {
        background: ${tokens.color.surface};
        border: 1px solid ${tokens.color.border};
        border-radius: ${tokens.radius.md};
        padding: 16px;
        box-shadow: ${tokens.shadow.card};
    }
    h3 {
        margin: 0 0 2px;
        font-size: 15px;
        font-weight: 650;
        display: flex;
        justify-content: space-between;
        gap: 8px;
    }
    p {
        margin: 8px 0 0;
        color: ${tokens.color.textMuted};
        font-size: 13px;
        display: -webkit-box;
        -webkit-line-clamp: 4;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }
`;

const Row = styled.article`
    display: grid;
    grid-template-columns: 56px 1fr auto;
    gap: 14px;
    padding: 14px 16px;
    border-bottom: 1px solid #eef0f5;
    &:last-child {
        border-bottom: 0;
    }
    .id {
        font: 600 12px ui-monospace, Menlo, monospace;
        color: ${tokens.color.textMuted};
        padding-top: 2px;
    }
    h3 {
        margin: 0;
        font-size: 14px;
        font-weight: 600;
    }
    p {
        margin: 4px 0 0;
        font-size: 13px;
        color: ${tokens.color.textMuted};
    }
`;

const Areas = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 12px;
    margin-bottom: 28px;
    a {
        display: flex;
        gap: 12px;
        align-items: center;
        padding: 12px 14px;
        background: ${tokens.color.surface};
        border: 1px solid ${tokens.color.border};
        border-radius: ${tokens.radius.md};
        color: ${tokens.color.text};
        transition: box-shadow 0.15s ease, transform 0.15s ease;
    }
    a:hover {
        box-shadow: ${tokens.shadow.hover};
        transform: translateY(-1px);
    }
`;

/** Everything the platform does, from ROADMAP.md: its layers, every roadmap item with its status, and where each area lives here. */
export function CapabilitiesPage() {
    const t = useT();
    const { layers, items } = data as { layers: Layer[]; items: Item[] };
    const [filter, setFilter] = useState<string>('all');
    const [q, setQ] = useState('');
    const shown = useMemo(() => {
        const text = q.trim().toLowerCase();
        return items
            .filter((i) => filter === 'all' || i.status === filter)
            .filter((i) => !text || `${i.id} ${i.title} ${i.summary} ${i.done} ${i.note}`.toLowerCase().includes(text))
            .slice()
            .reverse();
    }, [items, filter, q]);
    const count = (s: string) => items.filter((i) => i.status === s).length;
    return (
        <Page>
            <header style={{ display: 'flex', gap: 16, alignItems: 'flex-start', marginBottom: 20 }}>
                <HubIcon><Icon name="grid" size={22} /></HubIcon>
                <div>
                    <h1 style={{ margin: 0, fontSize: 26, fontWeight: 700 }}>{t('nav.capabilities')}</h1>
                    <Muted>Everything Cairn does today and how far each piece is proven, from the roadmap: each item has a definition of done tested end to end.</Muted>
                </div>
            </header>
            <Stats>
                <div><strong>{layers.length}</strong><Muted>platform layers</Muted></div>
                <div><strong>{items.length}</strong><Muted>roadmap items</Muted></div>
                <div><strong style={{ color: tokens.color.good }}>{count('shipped')}</strong><Muted>shipped and tested</Muted></div>
                <div><strong style={{ color: tokens.color.warn }}>{count('partial')}</strong><Muted>partly validated</Muted></div>
            </Stats>
            <h2 style={{ fontSize: 17, fontWeight: 650 }}>Where things are in the workspace</h2>
            <Areas>
                {NAV.filter((n) => n.to !== '/capabilities').map((n) => {
                    const hub = HUBS.find((h) => h.route === n.to);
                    return (
                        <Link key={n.to} to={n.to}>
                            <HubIcon style={{ width: 36, height: 36, borderRadius: 10 }}><Icon name={n.icon} /></HubIcon>
                            <span>
                                <strong style={{ display: 'block', fontWeight: 600 }}>{t(n.label)}</strong>
                                {hub && <Muted style={{ fontSize: 12 }}>{hub.tabs.length} {hub.tabs.length === 1 ? 'area' : 'areas'}</Muted>}
                            </span>
                        </Link>
                    );
                })}
            </Areas>
            <h2 style={{ fontSize: 17, fontWeight: 650 }}>Platform layers</h2>
            <Layers>
                {layers.map((l) => (
                    <article key={l.layer}>
                        <h3>{l.layer}<Tag color={STATUS[l.status].color}>{STATUS[l.status].label}</Tag></h3>
                        <Muted style={{ fontSize: 12 }}>Instead of {l.compare}</Muted>
                        <p title={l.cairn}>{l.cairn}</p>
                    </article>
                ))}
            </Layers>
            <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap', margin: '8px 0 12px' }}>
                <h2 style={{ fontSize: 17, fontWeight: 650, margin: 0, flex: 1 }}>Roadmap items</h2>
                <Input.Search allowClear placeholder="Filter items" aria-label="Filter roadmap items" value={q}
                    onChange={(e) => setQ(e.target.value)} style={{ width: 260 }} />
                <Radio.Group value={filter} onChange={(e) => setFilter(e.target.value)} optionType="button" buttonStyle="solid"
                    aria-label="Show items"
                    options={[{ label: `All ${items.length}`, value: 'all' }, { label: `Shipped ${count('shipped')}`, value: 'shipped' },
                        { label: `Partial ${count('partial')}`, value: 'partial' }]} />
            </div>
            <section style={{ background: tokens.color.surface, border: `1px solid ${tokens.color.border}`, borderRadius: 12 }}>
                {shown.map((i) => (
                    <Row key={i.id}>
                        <span className="id">{i.id}</span>
                        <div>
                            <h3>{i.title}</h3>
                            <p>{i.done}</p>
                            {i.status !== 'shipped' && i.note && <p style={{ color: tokens.color.warn }}>{i.note}</p>}
                        </div>
                        <div><Tag color={STATUS[i.status].color}>{STATUS[i.status].label}</Tag></div>
                    </Row>
                ))}
                {shown.length === 0 && <p style={{ padding: 24, margin: 0 }}><Muted>No item matches.</Muted></p>}
            </section>
        </Page>
    );
}
