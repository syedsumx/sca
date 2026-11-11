import React from 'react';
import { Link } from 'react-router-dom';
import styled from 'styled-components';

import { useDheQuery } from '../api/hooks';
import { useGuide } from '../guide/GuideProvider';
import { progress } from '../guide/onboarding';
import { useT } from '../i18n';
import type { PipelineSummary } from './PipelinesObs';
import { StatusBadge } from './StatusBadge';
import { tokens } from './tokens';

export const Card = styled.section`
    background: ${tokens.color.surface};
    border: 1px solid ${tokens.color.border};
    border-radius: ${tokens.radius.lg};
    padding: 20px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
    > header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 12px;
        flex-wrap: wrap;
    }
    h2 {
        margin: 0;
        font-size: 16px;
        font-weight: 600;
    }
`;

const Bar = styled.div<{ $pct: number }>`
    height: 6px;
    border-radius: 3px;
    background: ${tokens.color.border};
    > span {
        display: block;
        height: 6px;
        width: ${(p) => p.$pct}%;
        border-radius: 3px;
        background: ${tokens.color.primary};
    }
`;

const Tasks = styled.ul`
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 12px;
    a {
        display: flex;
        gap: 10px;
        align-items: flex-start;
        min-height: ${tokens.control}px;
        padding: 12px;
        border-radius: ${tokens.radius.md};
        background: ${tokens.color.surfaceSunken};
        color: ${tokens.color.text};
        font-size: 14px;
        line-height: 1.4;
        text-decoration: none;
    }
    a:hover {
        background: ${tokens.color.primarySoft};
    }
    .tick {
        flex: none;
        width: 20px;
        height: 20px;
        border-radius: 50%;
        box-sizing: border-box;
        border: 2px solid ${tokens.color.waiting};
        display: inline-grid;
        place-items: center;
        color: #fff;
        font-size: 12px;
        line-height: 1;
    }
    [data-done='true'] .tick {
        border-color: ${tokens.color.primary};
        background: ${tokens.color.primary};
    }
    [data-done='true'] .text {
        color: ${tokens.color.textMuted};
    }
`;

/** Home: the role's "Get started" checklist, compact, while there is something left to do. */
export function GetStartedCard() {
    const g = useGuide();
    const t = useT();
    const { role, done, dismissed } = g.ui.onboarding;
    if (!g.available || !g.enabled || !role || dismissed) return null;
    const p = progress(role, done);
    if (!p.total || p.complete) return null;
    const pct = Math.round((100 * p.done) / p.total);
    return (
        <Card aria-labelledby="home-gs">
            <header>
                <h2 id="home-gs">{t('home.getStarted')}</h2>
                <span style={{ fontSize: 13, color: tokens.color.textMuted }}>{t('guide.progress', { done: p.done, total: p.total })}</span>
            </header>
            <Bar $pct={pct} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}
                aria-label={t('guide.progress', { done: p.done, total: p.total })}><span /></Bar>
            <Tasks>
                {p.steps.map((s) => (
                    <li key={s.id}>
                        <Link to={s.to} data-done={s.done ? 'true' : 'false'}>
                            <span className="tick" aria-hidden="true">{s.done ? '✓' : ''}</span>
                            <span className="text">
                                {t(s.title)}
                                <span className="sr-only"> ({t(s.done ? 'guide.stepDone' : 'guide.stepOpen')})</span>
                            </span>
                        </Link>
                    </li>
                ))}
            </Tasks>
            <Link to="/get-started" style={{ fontSize: 14, fontWeight: 500 }}>{t('home.openChecklist')}</Link>
        </Card>
    );
}

const ORDER: Record<string, number> = { failing: 0, sla_missed: 1, anomalous: 2, unknown: 3, healthy: 4 };
const HEALTH_TONE = { healthy: 'ok', failing: 'error', sla_missed: 'warn', anomalous: 'warn', unknown: 'waiting' } as const;

/** Home: the pipelines that need attention first. Only for people who may read pipeline observability. */
export function PipelinesCard() {
    const t = useT();
    const q = useDheQuery<{ items: PipelineSummary[] }>('/v1/observability/pipelines', (c, signal) =>
        c.request('GET', '/v1/observability/pipelines', undefined, { signal }), { pollMs: 60_000 });
    if (q.error || !q.data) return null;                 // not an operator, or not set up: the card is not for them
    const rows = q.data.items.slice().sort((a, b) => (ORDER[a.health] ?? 3) - (ORDER[b.health] ?? 3)).slice(0, 5);
    return (
        <Card aria-labelledby="home-pipes">
            <header><h2 id="home-pipes">{t('home.pipelines')}</h2></header>
            {rows.length === 0 ? <span style={{ color: tokens.color.textMuted }}>{t('home.pipelinesNone')}</span> : (
                <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 10 }}>
                    {rows.map((p) => (
                        <li key={p.key} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                            <Link to={`/runs?tab=pipelines&pipeline=${encodeURIComponent(p.key)}`}
                                style={{ fontSize: 14, minHeight: 24, overflow: 'hidden', textOverflow: 'ellipsis' }}>{p.name}</Link>
                            <StatusBadge tone={HEALTH_TONE[p.health] ?? 'waiting'} label={t(`pipe.health.${p.health}` as never)} />
                        </li>
                    ))}
                </ul>
            )}
            <Link to="/runs?tab=pipelines" style={{ fontSize: 14, fontWeight: 500 }}>{t('home.pipelinesOpen')}</Link>
        </Card>
    );
}
