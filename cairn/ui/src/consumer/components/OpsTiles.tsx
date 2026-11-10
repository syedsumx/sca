import { Button, Checkbox, Input, InputNumber, Modal, Select, Space, Table, Tag } from 'antd';
import React, { useState } from 'react';
import { Link } from 'react-router-dom';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { DashboardControl, OpsDisplay, OpsResult, OpsSeries, OpsSource, OpsTile, OpsView } from '../api/types';
import { ErrorNotice, notifyError } from './ErrorNotice';
import { Muted, tokens } from './tokens';

/** Ops tiles (cairn.observability.dashboards): their results drawn by view, and the friendly form that makes one. */

export const OPS_SOURCES: { value: OpsSource; label: string; help: string }[] = [
    { value: 'promql', label: 'Metrics (PromQL)', help: 'A Prometheus query over the range, or its value now.' },
    { value: 'logql', label: 'Logs (LogQL)', help: 'Log lines from Loki, or a LogQL metric query over the range.' },
    { value: 'traces', label: 'Traces (TraceQL)', help: 'Requests that match a TraceQL search, each linked to its journey.' },
    { value: 'slo', label: 'SLO', help: 'An SLO’s SLI, error budget left and burn rates, and its burn over the range.' },
    { value: 'alerts', label: 'Alerts', help: 'Alerts that are firing (or pending), filtered by labels.' },
    { value: 'postgres', label: 'PostgreSQL instance', help: 'An instance’s connections, cache hits, commits, locks and size.' },
];
export const VIEWS_OF: Record<OpsSource, OpsView[]> = {
    promql: ['timeseries', 'area', 'bars', 'stat', 'gauge', 'table', 'heatmap'],
    logql: ['logs', 'timeseries', 'area', 'bars', 'stat', 'table'],
    traces: ['table'],
    slo: ['stat', 'timeseries'],
    alerts: ['table'],
    postgres: ['stat', 'table'],
};
const VIEW_LABEL: Record<OpsView, string> = { timeseries: 'Time series', area: 'Area', bars: 'Bars', stat: 'Stat',
    gauge: 'Gauge', table: 'Table', logs: 'Logs', heatmap: 'Heatmap' };
export const UNITS = [
    { value: 'short', label: 'Number' }, { value: 'percent', label: 'Percent (0–100)' }, { value: 'percentunit', label: 'Percent (0–1)' },
    { value: 'ms', label: 'Milliseconds' }, { value: 's', label: 'Seconds' }, { value: 'bytes', label: 'Bytes' },
    { value: 'Bps', label: 'Bytes / s' }, { value: 'reqps', label: 'Requests / s' }, { value: 'ops', label: 'Operations / s' },
];
const COLORS: Record<string, string> = { green: '#2f9e44', red: '#c92a2a', orange: '#e8590c', yellow: '#f08c00', blue: '#1971c2',
    purple: '#7048e8', grey: '#868e96', text: tokens.color.text };
const SERIES_COLORS = ['#3b5bdb', '#2f9e44', '#e8590c', '#ae3ec9', '#1098ad', '#f08c00', '#c2255c', '#5c940d', '#495057', '#d6336c'];
export const RANGES = ['5m', '15m', '1h', '6h', '24h', '7d'];
export const REFRESHES = [{ value: 0, label: 'Off' }, { value: 10, label: '10s' }, { value: 30, label: '30s' },
    { value: 60, label: '1m' }, { value: 300, label: '5m' }];

export const sourceOf = (t: Partial<OpsTile>): OpsSource | null =>
    (['promql', 'logql', 'traces', 'slo', 'alerts', 'postgres'] as OpsSource[]).find((k) => t[k] !== undefined && t[k] !== null) ?? null;

const color = (c: string) => COLORS[c] ?? c;

/** A value in its unit: 0.123 percentunit → "12.3%", 1536 bytes → "1.5 KiB", 1500 ms → "1.5 s". */
export function formatValue(v: number | null | undefined, unit?: string, decimals?: number): string {
    if (v === null || v === undefined || Number.isNaN(v)) return '–';
    const d = (x: number, def = 2) => Number(x.toFixed(decimals ?? def)).toLocaleString(undefined, { maximumFractionDigits: decimals ?? def });
    const scaled = (x: number, steps: [number, string][], suffix = '') => {
        let i = 0;
        while (i < steps.length - 1 && Math.abs(x) >= steps[i + 1][0]) i++;
        return `${d(x / steps[i][0])} ${steps[i][1]}${suffix}`;
    };
    const bytes: [number, string][] = [[1, 'B'], [1024, 'KiB'], [1024 ** 2, 'MiB'], [1024 ** 3, 'GiB'], [1024 ** 4, 'TiB']];
    switch (unit) {
        case 'percent': return `${d(v, 1)}%`;
        case 'percentunit': return `${d(v * 100, 1)}%`;
        case 'ms': return Math.abs(v) >= 1000 ? `${d(v / 1000)} s` : `${d(v)} ms`;
        case 's': return Math.abs(v) < 1 ? `${d(v * 1000)} ms` : Math.abs(v) >= 3600 ? `${d(v / 3600, 1)} h` : Math.abs(v) >= 60 ? `${d(v / 60, 1)} min` : `${d(v)} s`;
        case 'bytes': case 'decbytes': return scaled(v, bytes);
        case 'Bps': return scaled(v, bytes, '/s');
        case 'reqps': case 'rps': return `${d(v)} req/s`;
        case 'ops': return `${d(v)} ops/s`;
        case 'cps': return `${d(v)} /s`;
        default:
            return Math.abs(v) >= 1e9 ? `${d(v / 1e9, 1)}B` : Math.abs(v) >= 1e6 ? `${d(v / 1e6, 1)}M` : Math.abs(v) >= 1e4 ? `${d(v / 1e3, 1)}k` : d(v);
    }
}

/** The colour a value takes from its thresholds (the last step at or below it; null is the base). */
export function thresholdColor(v: number | null | undefined, display?: OpsDisplay): string | undefined {
    const steps = display?.thresholds ?? [];
    if (!steps.length || v === null || v === undefined) return undefined;
    let c = steps[0].color;
    for (const s of steps) if (s.value === null || v >= s.value) c = s.color;
    return color(c);
}

const lastValue = (s: OpsSeries): number | null => {
    for (let i = s.points.length - 1; i >= 0; i--) if (s.points[i][1] !== null) return s.points[i][1];
    return null;
};
const time = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

/** Lines, areas or stacked bars of series over time, with the thresholds as dashed lines and a legend. */
export function TimeSeries({ series, view, display, title, start, end }: {
    series: OpsSeries[]; view: OpsView; display?: OpsDisplay; title: string; start?: number; end?: number;
}) {
    const W = 720, H = 200, L = 64, B = 22;
    const drawn = series.slice(0, 20);
    if (!drawn.length || drawn.every((s) => !s.points.length)) return <Muted>No data in this range.</Muted>;
    const xs = drawn.flatMap((s) => s.points.map((p) => p[0]));
    const x0 = start ?? Math.min(...xs), x1 = Math.max(end ?? Math.max(...xs), x0 + 1);
    const stackedAt = new Map<number, number>();
    if (view === 'bars') for (const s of drawn) for (const [t, v] of s.points) stackedAt.set(t, (stackedAt.get(t) ?? 0) + Math.max(0, v ?? 0));
    const vals = view === 'bars' ? [...stackedAt.values()] : drawn.flatMap((s) => s.points.map((p) => p[1]).filter((v): v is number => v !== null));
    const thr = (display?.thresholds ?? []).map((t) => t.value).filter((v): v is number => v !== null);
    const y0 = Math.min(display?.min ?? Infinity, 0, ...vals), y1 = Math.max(display?.max ?? -Infinity, ...vals, ...thr, y0 + 1e-9);
    const px = (t: number) => L + ((t - x0) / (x1 - x0)) * (W - L - 8);
    const py = (v: number) => 6 + (1 - (v - y0) / (y1 - y0)) * (H - B - 6);
    const ticks = [y0, (y0 + y1) / 2, y1];
    const base: Map<number, number> = new Map();
    const barW = Math.max(1, ((W - L - 8) / Math.max(1, new Set(xs).size)) * 0.8);
    return (
        <div>
            <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}: ${VIEW_LABEL[view].toLowerCase()} of ${drawn.length} series`}
                style={{ width: '100%', maxWidth: W }}>
                {ticks.map((v, i) => (
                    <g key={i}>
                        <line x1={L} x2={W - 8} y1={py(v)} y2={py(v)} stroke={tokens.color.border} />
                        <text x={L - 6} y={py(v) + 4} fontSize={11} textAnchor="end" fill={tokens.color.textMuted}>{formatValue(v, display?.unit, display?.decimals)}</text>
                    </g>
                ))}
                <text x={L} y={H - 4} fontSize={11} fill={tokens.color.textMuted}>{time(x0)}</text>
                <text x={W - 8} y={H - 4} fontSize={11} textAnchor="end" fill={tokens.color.textMuted}>{time(x1)}</text>
                {(display?.thresholds ?? []).filter((t) => t.value !== null).map((t, i) => (
                    <line key={`t${i}`} x1={L} x2={W - 8} y1={py(t.value as number)} y2={py(t.value as number)} stroke={color(t.color)}
                        strokeDasharray="4 4" />
                ))}
                {drawn.map((s, si) => {
                    const c = SERIES_COLORS[si % SERIES_COLORS.length];
                    if (view === 'bars') {
                        return (
                            <g key={si}>
                                {s.points.filter((p) => p[1] !== null && (p[1] as number) > 0).map(([t, v]) => {
                                    const b = base.get(t) ?? 0;
                                    base.set(t, b + (v as number));
                                    return <rect key={t} x={px(t) - barW / 2} width={barW} y={py(b + (v as number))} height={Math.max(0, py(b) - py(b + (v as number)))} fill={c}>
                                        <title>{`${s.name} ${time(t)}: ${formatValue(v, display?.unit, display?.decimals)}`}</title>
                                    </rect>;
                                })}
                            </g>
                        );
                    }
                    const pts = s.points.filter((p) => p[1] !== null) as [number, number][];
                    if (!pts.length) return null;
                    const d = pts.map(([t, v], i) => `${i ? 'L' : 'M'}${px(t).toFixed(1)},${py(v).toFixed(1)}`).join(' ');
                    return (
                        <g key={si}>
                            {view === 'area' && <path d={`${d} L${px(pts[pts.length - 1][0])},${py(y0)} L${px(pts[0][0])},${py(y0)} Z`} fill={c} opacity={0.18} />}
                            <path d={d} fill="none" stroke={c} strokeWidth={1.6}><title>{s.name}</title></path>
                        </g>
                    );
                })}
            </svg>
            <ul aria-label={`${title}: legend`} style={{ listStyle: 'none', padding: 0, margin: '4px 0 0', display: 'flex', flexWrap: 'wrap', gap: '4px 14px', fontSize: 12 }}>
                {drawn.map((s, si) => (
                    <li key={si}>
                        <span aria-hidden style={{ display: 'inline-block', width: 10, height: 10, borderRadius: 2, marginRight: 4, background: SERIES_COLORS[si % SERIES_COLORS.length] }} />
                        {s.name || '(no labels)'}: <strong>{formatValue(lastValue(s), display?.unit, display?.decimals)}</strong>
                    </li>
                ))}
                {series.length > drawn.length && <li><Muted>and {series.length - drawn.length} more</Muted></li>}
            </ul>
        </div>
    );
}

/** One big number (the last value), coloured by the thresholds. */
export function Stat({ value, label, display }: { value: number | null; label: string; display?: OpsDisplay }) {
    const c = thresholdColor(value, display);
    return (
        <div role="img" aria-label={`${label}: ${formatValue(value, display?.unit, display?.decimals)}`} style={{ padding: '8px 4px', minWidth: 120 }}>
            <div style={{ fontSize: 34, fontWeight: 600, lineHeight: 1.15, color: c ?? tokens.color.text }}>{formatValue(value, display?.unit, display?.decimals)}</div>
            <div style={{ color: tokens.color.textMuted, fontSize: 12 }}>{label}</div>
        </div>
    );
}

/** A half-circle gauge from min to max (by default 0 to 1 for a ratio, 0 to 100 for a percentage). */
export function Gauge({ value, label, display }: { value: number | null; label: string; display?: OpsDisplay }) {
    const min = display?.min ?? 0;
    const max = display?.max ?? (display?.unit === 'percentunit' ? 1 : display?.unit === 'percent' ? 100 : Math.max(1, (value ?? 0) * 1.25));
    const f = value === null ? 0 : Math.min(1, Math.max(0, (value - min) / (max - min || 1)));
    const R = 60, cx = 80, cy = 74;
    const at = (a: number) => [cx - R * Math.cos(a * Math.PI), cy - R * Math.sin(a * Math.PI)];
    const [ex, ey] = at(f);
    const c = thresholdColor(value, display) ?? tokens.color.primary;
    return (
        <svg viewBox="0 0 160 96" role="img" aria-label={`${label}: ${formatValue(value, display?.unit, display?.decimals)} of ${formatValue(max, display?.unit)}`}
            style={{ width: 200, maxWidth: '100%' }}>
            <path d={`M${cx - R},${cy} A${R},${R} 0 0 1 ${cx + R},${cy}`} fill="none" stroke={tokens.color.border} strokeWidth={12} />
            {f > 0 && <path d={`M${cx - R},${cy} A${R},${R} 0 0 1 ${ex.toFixed(2)},${ey.toFixed(2)}`} fill="none" stroke={c} strokeWidth={12} />}
            <text x={cx} y={cy - 6} textAnchor="middle" fontSize={18} fontWeight={600} fill={tokens.color.text}>{formatValue(value, display?.unit, display?.decimals)}</text>
            <text x={cx} y={cy + 16} textAnchor="middle" fontSize={10} fill={tokens.color.textMuted}>{label}</text>
        </svg>
    );
}

/** Buckets (rows, e.g. a histogram's le) by time (columns), darker where the value is higher. */
export function Heatmap({ series, title, display }: { series: OpsSeries[]; title: string; display?: OpsDisplay }) {
    const rows = [...series].sort((a, b) => (parseFloat(a.name) || 0) - (parseFloat(b.name) || 0)).slice(0, 30);
    const times = [...new Set(rows.flatMap((s) => s.points.map((p) => p[0])))].sort((a, b) => a - b);
    if (!rows.length || !times.length) return <Muted>No data in this range.</Muted>;
    const max = Math.max(1e-9, ...rows.flatMap((s) => s.points.map((p) => p[1] ?? 0)));
    const W = 720, L = 70, cw = (W - L) / times.length, ch = Math.max(8, Math.min(18, 200 / rows.length));
    const idx = new Map(times.map((t, i) => [t, i]));
    return (
        <svg viewBox={`0 0 ${W} ${rows.length * ch + 16}`} role="img" aria-label={`${title}: heatmap of ${rows.length} buckets over time`} style={{ width: '100%', maxWidth: W }}>
            {rows.map((s, r) => (
                <g key={r}>
                    <text x={L - 4} y={r * ch + ch * 0.75} fontSize={10} textAnchor="end" fill={tokens.color.textMuted}>{s.name}</text>
                    {s.points.map(([t, v]) => (
                        <rect key={t} x={L + (idx.get(t) ?? 0) * cw} y={r * ch} width={Math.max(1, cw - 0.5)} height={ch - 1}
                            fill={tokens.color.primary} opacity={0.08 + 0.92 * ((v ?? 0) / max)}>
                            <title>{`${s.name} ${time(t)}: ${formatValue(v, display?.unit === 'ms' ? undefined : display?.unit)}`}</title>
                        </rect>
                    ))}
                </g>
            ))}
            <text x={L} y={rows.length * ch + 12} fontSize={10} fill={tokens.color.textMuted}>{time(times[0])}</text>
            <text x={W} y={rows.length * ch + 12} fontSize={10} textAnchor="end" fill={tokens.color.textMuted}>{time(times[times.length - 1])}</text>
        </svg>
    );
}

const SEVERITY: Record<string, string> = { critical: 'red', warning: 'orange', info: 'blue' };

/** An ops tile's result, drawn as its view. */
export function OpsTileView({ tile }: { tile: OpsTile & { ops?: OpsResult } }) {
    const r = tile.ops;
    const view = tile.view ?? 'timeseries';
    const display = tile.display;
    const title = tile.title || 'Tile';
    if (!r) return null;
    if (r.kind === 'series') {
        if (view === 'stat' || view === 'gauge') {
            const s = r.series[0];
            const v = s ? lastValue(s) : null;
            const Comp = view === 'stat' ? Stat : Gauge;
            if (r.series.length <= 1) return <Comp value={v} label={s?.name || title} display={display} />;
            return <Space wrap>{r.series.slice(0, 12).map((x, i) => <Comp key={i} value={lastValue(x)} label={x.name} display={display} />)}</Space>;
        }
        if (view === 'table') {
            return <Table size="small" rowKey="name" pagination={{ pageSize: 10, hideOnSinglePage: true }}
                dataSource={r.series.map((s) => ({ name: s.name, last: lastValue(s),
                    min: Math.min(...s.points.map((p) => p[1] ?? Infinity)), max: Math.max(...s.points.map((p) => p[1] ?? -Infinity)) }))}
                columns={[{ title: 'Series', dataIndex: 'name' },
                    { title: 'Last', dataIndex: 'last', render: (v: number) => <span style={{ color: thresholdColor(v, display) }}>{formatValue(v, display?.unit, display?.decimals)}</span> },
                    ...(r.instant ? [] : [{ title: 'Min', dataIndex: 'min', render: (v: number) => formatValue(Number.isFinite(v) ? v : null, display?.unit, display?.decimals) },
                        { title: 'Max', dataIndex: 'max', render: (v: number) => formatValue(Number.isFinite(v) ? v : null, display?.unit, display?.decimals) }])]} />;
        }
        if (view === 'heatmap') return <Heatmap series={r.series} title={title} display={display} />;
        return <TimeSeries series={r.series} view={view} display={display} title={title} start={r.start_ms} end={r.end_ms} />;
    }
    if (r.kind === 'logs') {
        if (!r.lines.length) return <Muted>No log lines in this range.</Muted>;
        return (
            <ol aria-label={`${title}: log lines`} style={{ listStyle: 'none', padding: 0, margin: 0, maxHeight: 280, overflow: 'auto',
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 12 }}>
                {r.lines.map((l, i) => (
                    <li key={i} style={{ borderBottom: `1px solid ${tokens.color.border}`, padding: '2px 0', wordBreak: 'break-word' }}>
                        <Muted>{new Date(l.ts_ms).toLocaleTimeString()}</Muted>{' '}
                        {l.level && <Tag color={/err|crit|fatal/i.test(l.level) ? 'red' : /warn/i.test(l.level) ? 'orange' : 'default'}>{l.level}</Tag>}
                        {l.service && <Muted>{l.service} </Muted>}
                        {l.line}
                        {l.trace_id && <> <Link to={`/observability?tab=journey&trace=${l.trace_id}`} aria-label={`Journey of trace ${l.trace_id}`}>journey</Link></>}
                    </li>
                ))}
            </ol>
        );
    }
    if (r.kind === 'traces') {
        return <Table size="small" rowKey="trace_id" pagination={{ pageSize: 10, hideOnSinglePage: true }} dataSource={r.traces}
            locale={{ emptyText: 'No matching requests in this range.' }}
            columns={[{ title: 'Request', render: (_: unknown, t) => <Link to={t.journey} aria-label={`Journey of ${t.root_name ?? t.trace_id}`}>{t.root_name ?? t.trace_id}</Link> },
                { title: 'Service', dataIndex: 'root_service' },
                { title: 'Duration', dataIndex: 'duration_ms', render: (v: number) => formatValue(v, 'ms') },
                { title: 'When', dataIndex: 'start_ms', render: (v: number) => new Date(v).toLocaleTimeString() }]} />;
    }
    if (r.kind === 'alerts') {
        return <Table size="small" rowKey="id" pagination={{ pageSize: 10, hideOnSinglePage: true }} dataSource={r.alerts}
            locale={{ emptyText: 'Nothing firing.' }}
            columns={[{ title: '', dataIndex: 'severity', render: (v: string) => <Tag color={SEVERITY[v]}>{v}</Tag> },
                { title: 'Alert', dataIndex: 'name', render: (v: string, a) => (a.incident_id ? <Link to={`/observability?tab=incidents&incident=${a.incident_id}`}>{v}</Link> : v) },
                { title: 'State', dataIndex: 'state', render: (v: string, a) => <>{v}{a.silenced_by && <Tag>silenced</Tag>}{a.acknowledged_by && <Tag>acknowledged</Tag>}</> },
                { title: 'Labels', dataIndex: 'labels', render: (v: Record<string, string>) => Object.entries(v).map(([k, x]) => `${k}=${x}`).join(', ') },
                { title: 'Summary', dataIndex: 'summary' }]} />;
    }
    if (r.kind === 'slo') {
        if (view === 'timeseries') return <TimeSeries series={r.series} view="timeseries" display={display} title={title} start={r.start_ms} end={r.end_ms} />;
        const st = r.status;
        return (
            <Space wrap align="start">
                <Stat value={st.sli === null ? null : st.sli} label={`${r.slo.name}: SLI (objective ${st.objective}%)`} display={{ unit: 'percent', decimals: 3,
                    thresholds: [{ value: null, color: 'red' }, { value: st.objective, color: 'green' }] }} />
                <Stat value={st.budget_remaining} label={`error budget left (${st.window})`} display={{ unit: 'percentunit',
                    thresholds: [{ value: null, color: 'red' }, { value: 0, color: 'orange' }, { value: 0.25, color: 'green' }] }} />
                <Stat value={r.firing} label="burn alerts firing" display={{ thresholds: [{ value: null, color: 'green' }, { value: 1, color: 'red' }] }} />
                <ul aria-label={`${title}: burn rates`} style={{ margin: 0, paddingLeft: 16, fontSize: 12 }}>
                    {st.burn.map((b, i) => (
                        <li key={i}>{b.long}/{b.short}: {formatValue(b.long_burn)}× / {formatValue(b.short_burn)}× (over {b.factor}×){b.burning && <Tag color="red" style={{ marginLeft: 4 }}>burning</Tag>}</li>
                    ))}
                </ul>
            </Space>
        );
    }
    // postgres
    if (view === 'table') {
        return <Table size="small" rowKey="database" pagination={{ pageSize: 10, hideOnSinglePage: true }} dataSource={r.databases}
            columns={[{ title: 'Database', dataIndex: 'database' }, { title: 'Connections', dataIndex: 'connections' },
                { title: 'Commits', dataIndex: 'commits' }, { title: 'Rollbacks', dataIndex: 'rollbacks' },
                { title: 'Cache hits', dataIndex: 'cache_hit_ratio', render: (v: number | null) => formatValue(v, 'percentunit') },
                { title: 'Size', dataIndex: 'size_bytes', render: (v: number | null) => formatValue(v, 'bytes') }]} />;
    }
    return (
        <div>
            <Space wrap>{r.values.map((v) => <Stat key={v.name} value={v.value} label={v.name} display={{ unit: v.unit ?? display?.unit }} />)}</Space>
            {r.series.length > 0 && <TimeSeries series={r.series} view="timeseries" display={display} title={title} />}
        </div>
    );
}

// ---- the tile editor -----------------------------------------------------------------------------------------------
type Draft = {
    source: OpsSource; title: string; width: 1 | 2; view: OpsView; unit: string; decimals: number | null;
    min: number | null; max: number | null; thresholds: { value: number | null; color: string }[];
    queries: { expr: string; legend: string }[]; instant: boolean; step: string; mode: 'logs' | 'metric'; limit: number;
    traceql: string; slo: string; state: string; labels: string; severity: string; instance: string;
};

const EMPTY: Draft = { source: 'promql', title: '', width: 1, view: 'timeseries', unit: '', decimals: null, min: null, max: null,
    thresholds: [], queries: [{ expr: '', legend: '' }], instant: false, step: '', mode: 'logs', limit: 100, traceql: '',
    slo: '', state: 'firing', labels: '', severity: '', instance: '$instance' };

export function draftOf(t?: OpsTile): Draft {
    if (!t) return { ...EMPTY };
    const src = sourceOf(t) ?? 'promql';
    const d = t.display ?? {};
    const base: Draft = { ...EMPTY, source: src, title: t.title ?? '', width: t.width === 2 ? 2 : 1, view: t.view ?? VIEWS_OF[src][0],
        unit: d.unit ?? '', decimals: d.decimals ?? null, min: d.min ?? null, max: d.max ?? null, thresholds: d.thresholds ?? [] };
    if (t.promql) return { ...base, queries: t.promql.queries.map((q) => ({ expr: q.expr, legend: q.legend ?? '' })), instant: !!t.promql.instant, step: t.promql.step ?? '' };
    if (t.logql) return { ...base, queries: t.logql.queries.map((q) => ({ expr: q.expr, legend: q.legend ?? '' })), mode: t.logql.mode ?? 'logs', limit: t.logql.limit ?? 100, step: t.logql.step ?? '' };
    if (t.traces) return { ...base, traceql: t.traces.query, limit: t.traces.limit ?? 20 };
    if (t.slo) return { ...base, slo: t.slo.id };
    if (t.alerts) return { ...base, state: t.alerts.state ?? 'firing', severity: t.alerts.severity ?? '',
        labels: Object.entries(t.alerts.labels ?? {}).map(([k, v]) => `${k}=${v}`).join(', ') };
    if (t.postgres) return { ...base, instance: t.postgres.instance };
    return base;
}

/** The tile a draft stands for (the API checks it again). */
export function tileOf(d: Draft): OpsTile {
    const display: OpsDisplay = {
        ...(d.unit ? { unit: d.unit } : {}), ...(d.decimals !== null ? { decimals: d.decimals } : {}),
        ...(d.min !== null ? { min: d.min } : {}), ...(d.max !== null ? { max: d.max } : {}),
        ...(d.thresholds.length ? { thresholds: d.thresholds } : {}),
    };
    const queries = d.queries.filter((q) => q.expr.trim()).map((q) => ({ expr: q.expr.trim(), legend: q.legend.trim() }));
    const tile: OpsTile = { title: d.title.trim() || OPS_SOURCES.find((s) => s.value === d.source)!.label, width: d.width, view: d.view, display };
    if (d.source === 'promql') tile.promql = { queries, instant: d.instant, step: d.step.trim() || null };
    else if (d.source === 'logql') tile.logql = { queries, mode: d.mode, limit: d.limit, step: d.step.trim() || null };
    else if (d.source === 'traces') tile.traces = { query: d.traceql.trim(), limit: d.limit };
    else if (d.source === 'slo') tile.slo = { id: d.slo.trim() };
    else if (d.source === 'alerts') {
        const labels = Object.fromEntries(d.labels.split(',').map((x) => x.trim()).filter((x) => x.includes('='))
            .map((x) => [x.slice(0, x.indexOf('=')).trim(), x.slice(x.indexOf('=') + 1).trim()]));
        tile.alerts = { state: d.state as 'firing', labels, severity: (d.severity || null) as null };
    } else tile.postgres = { instance: d.instance.trim() };
    return tile;
}

const PLACEHOLDER: Record<OpsSource, string> = {
    promql: 'sum by (service_name) (rate(traces_span_metrics_calls_total{service_name="$service"}[$__rate_interval]))',
    logql: '{service_name="$service"} |= "error"',
    traces: '{ resource.service.name = "$service" && duration > 500ms }',
    slo: '', alerts: '', postgres: '',
};

/** The friendly form for one ops tile: its source and query, how it is drawn, and a preview through Cairn. */
export function OpsTileEditor({ open, initial, controls, values, onCancel, onSave }: {
    open: boolean; initial?: OpsTile; controls: DashboardControl[]; values: Record<string, unknown>;
    onCancel: () => void; onSave: (t: OpsTile) => void;
}) {
    const { client } = useConsumerApi();
    const [d, setD] = useState<Draft>(() => draftOf(initial));
    const [preview, setPreview] = useState<{ ops?: OpsResult; error?: unknown } | null>(null);
    const [busy, setBusy] = useState(false);
    const slos = useDheQuery<{ items: { id: string; name: string }[] }>(open && d.source === 'slo' ? '/v1/observability/slos' : null,
        (c, signal) => c.request('GET', '/v1/observability/slos', undefined, { signal }));
    const set = (patch: Partial<Draft>) => { setD({ ...d, ...patch }); setPreview(null); };
    const setSource = (source: OpsSource) => set({ source, view: VIEWS_OF[source][0], mode: 'logs', limit: source === 'traces' ? 20 : 100 });
    const tile = tileOf(d);
    const ready = d.source === 'promql' || d.source === 'logql' ? tile.promql?.queries.length || tile.logql?.queries.length
        : d.source === 'traces' ? !!d.traceql.trim() : d.source === 'slo' ? !!d.slo.trim() : d.source === 'postgres' ? !!d.instance.trim() : true;
    const run = async () => {
        setBusy(true);
        try {
            const out = await client.request<{ ops: OpsResult }>('POST', '/v1/observability/tiles/preview', { tile, controls, values });
            setPreview({ ops: out.ops });
        } catch (e) {
            setPreview({ error: e });
        } finally {
            setBusy(false);
        }
    };
    const source = OPS_SOURCES.find((s) => s.value === d.source)!;
    const vars = controls.filter((c) => c.kind === 'variable').map((c) => `$${c.id}`);
    return (
        <Modal title={initial ? 'Edit ops tile' : 'New ops tile'} open={open} width={820} onCancel={onCancel} okText={initial ? 'Save tile' : 'Add tile'}
            okButtonProps={{ disabled: !ready }} onOk={() => onSave(tile)} destroyOnClose>
            <Space direction="vertical" style={{ width: '100%' }}>
                <Space wrap>
                    <Select aria-label="Source" value={d.source} style={{ width: 210 }} onChange={setSource}
                        options={OPS_SOURCES.map((s) => ({ value: s.value, label: s.label }))} />
                    <Input aria-label="Tile title" placeholder="Title" value={d.title} style={{ width: 260 }} onChange={(e) => set({ title: e.target.value })} />
                    <Select aria-label="Tile width" value={d.width} style={{ width: 130 }} onChange={(width) => set({ width })}
                        options={[{ value: 1, label: 'Half width' }, { value: 2, label: 'Full width' }]} />
                </Space>
                <Muted>{source.help} {vars.length > 0 && `Variables: ${vars.join(', ')}; also $__interval, $__rate_interval and $__range.`}</Muted>
                {(d.source === 'promql' || d.source === 'logql') && (
                    <>
                        {d.queries.map((q, i) => (
                            <Space key={i} align="start" style={{ width: '100%' }} wrap>
                                <Input.TextArea aria-label={`Query ${i + 1}`} rows={2} style={{ width: 520, fontFamily: 'ui-monospace, monospace' }}
                                    placeholder={PLACEHOLDER[d.source]} value={q.expr}
                                    onChange={(e) => set({ queries: d.queries.map((x, j) => (j === i ? { ...x, expr: e.target.value } : x)) })} />
                                <Input aria-label={`Legend ${i + 1}`} placeholder="Legend, e.g. {{service_name}}" value={q.legend} style={{ width: 200 }}
                                    onChange={(e) => set({ queries: d.queries.map((x, j) => (j === i ? { ...x, legend: e.target.value } : x)) })} />
                                {d.queries.length > 1 && <Button aria-label={`Remove query ${i + 1}`} onClick={() => set({ queries: d.queries.filter((_, j) => j !== i) })}>×</Button>}
                            </Space>
                        ))}
                        <Space wrap>
                            {d.queries.length < 8 && <Button size="small" onClick={() => set({ queries: [...d.queries, { expr: '', legend: '' }] })}>Add query</Button>}
                            {d.source === 'promql' && <Checkbox checked={d.instant} onChange={(e) => set({ instant: e.target.checked })}>Value now (instant)</Checkbox>}
                            {d.source === 'logql' && (
                                <Select aria-label="Log mode" value={d.mode} style={{ width: 200 }} onChange={(mode) => set({ mode, view: mode === 'logs' ? 'logs' : 'timeseries' })}
                                    options={[{ value: 'logs', label: 'Log lines' }, { value: 'metric', label: 'Metric over time' }]} />
                            )}
                            {(d.source === 'promql' || d.mode === 'metric') && (
                                <Input aria-label="Step" placeholder="Step: auto" value={d.step} style={{ width: 120 }} onChange={(e) => set({ step: e.target.value })} />
                            )}
                            {d.source === 'logql' && d.mode === 'logs' && (
                                <InputNumber aria-label="Lines" min={1} max={5000} value={d.limit} addonAfter="lines" onChange={(v) => set({ limit: Number(v) || 100 })} />
                            )}
                        </Space>
                    </>
                )}
                {d.source === 'traces' && (
                    <Space wrap align="start">
                        <Input.TextArea aria-label="TraceQL" rows={2} style={{ width: 520, fontFamily: 'ui-monospace, monospace' }} placeholder={PLACEHOLDER.traces}
                            value={d.traceql} onChange={(e) => set({ traceql: e.target.value })} />
                        <InputNumber aria-label="Traces" min={1} max={500} value={d.limit} addonAfter="traces" onChange={(v) => set({ limit: Number(v) || 20 })} />
                    </Space>
                )}
                {d.source === 'slo' && (
                    <Select aria-label="SLO" showSearch placeholder="An SLO, or $slo" value={d.slo || undefined} style={{ width: 320 }}
                        onChange={(slo) => set({ slo })} loading={slos.loading}
                        options={[...(slos.data?.items ?? []).map((s) => ({ value: s.id, label: s.name })),
                            ...vars.map((v) => ({ value: v, label: v }))]} />
                )}
                {d.source === 'alerts' && (
                    <Space wrap>
                        <Select aria-label="Alert state" value={d.state} style={{ width: 180 }} onChange={(state) => set({ state })}
                            options={[{ value: 'firing', label: 'Firing' }, { value: 'open', label: 'Firing or pending' }, { value: 'pending', label: 'Pending' }, { value: 'resolved', label: 'Resolved' }]} />
                        <Select aria-label="Severity" value={d.severity} style={{ width: 150 }} onChange={(severity) => set({ severity })}
                            options={[{ value: '', label: 'Any severity' }, { value: 'critical', label: 'Critical' }, { value: 'warning', label: 'Warning' }, { value: 'info', label: 'Info' }]} />
                        <Input aria-label="Labels" placeholder="Labels: service=$service, team=data" value={d.labels} style={{ width: 300 }} onChange={(e) => set({ labels: e.target.value })} />
                    </Space>
                )}
                {d.source === 'postgres' && (
                    <Input aria-label="Instance" placeholder="An instance's name, or $instance" value={d.instance} style={{ width: 260 }} onChange={(e) => set({ instance: e.target.value })} />
                )}
                <strong>Display</strong>
                <Space wrap>
                    <Select aria-label="View" value={d.view} style={{ width: 150 }} onChange={(view) => set({ view })}
                        options={VIEWS_OF[d.source].filter((v) => v !== 'logs' || d.mode === 'logs').filter((v) => d.source !== 'logql' || d.mode === 'metric' || v === 'logs')
                            .map((v) => ({ value: v, label: VIEW_LABEL[v] }))} />
                    <Select aria-label="Unit" allowClear placeholder="Unit" value={d.unit || undefined} style={{ width: 170 }} onChange={(unit) => set({ unit: unit ?? '' })} options={UNITS} />
                    <InputNumber aria-label="Decimals" placeholder="Decimals" min={0} max={6} value={d.decimals} onChange={(v) => set({ decimals: v === null ? null : Number(v) })} />
                    <InputNumber aria-label="Minimum" placeholder="Min" value={d.min} onChange={(v) => set({ min: v === null ? null : Number(v) })} />
                    <InputNumber aria-label="Maximum" placeholder="Max" value={d.max} onChange={(v) => set({ max: v === null ? null : Number(v) })} />
                </Space>
                <Space wrap>
                    <Muted>Thresholds:</Muted>
                    {d.thresholds.map((t, i) => (
                        <Space key={i} size={2}>
                            <InputNumber aria-label={`Threshold ${i + 1} from`} placeholder={i === 0 ? 'base' : 'from'} value={t.value} style={{ width: 100 }}
                                onChange={(v) => set({ thresholds: d.thresholds.map((x, j) => (j === i ? { ...x, value: v === null ? null : Number(v) } : x)) })} />
                            <Select aria-label={`Threshold ${i + 1} colour`} value={t.color} style={{ width: 100 }}
                                onChange={(c) => set({ thresholds: d.thresholds.map((x, j) => (j === i ? { ...x, color: c } : x)) })}
                                options={['green', 'orange', 'red', 'yellow', 'blue', 'purple', 'grey'].map((c) => ({ value: c, label: c }))} />
                            <Button aria-label={`Remove threshold ${i + 1}`} size="small" onClick={() => set({ thresholds: d.thresholds.filter((_, j) => j !== i) })}>×</Button>
                        </Space>
                    ))}
                    <Button size="small" onClick={() => set({ thresholds: [...d.thresholds, d.thresholds.length ? { value: 0, color: 'red' } : { value: null, color: 'green' }] })}>
                        Add threshold
                    </Button>
                </Space>
                <Space>
                    <Button onClick={() => void run()} loading={busy} disabled={!ready}>Preview</Button>
                    <Muted>Runs through Cairn as you, over the dashboard&apos;s range and variables.</Muted>
                </Space>
                {preview?.error !== undefined && <ErrorNotice error={preview.error} action="Preview the tile" />}
                {preview?.ops && (
                    <section aria-label="Preview" style={{ border: `1px solid ${tokens.color.border}`, borderRadius: 8, padding: 8 }}>
                        <OpsTileView tile={{ ...tile, ops: preview.ops }} />
                    </section>
                )}
            </Space>
        </Modal>
    );
}

// ---- the ops dashboard editor ---------------------------------------------------------------------------------------
type VarDraft = { id: string; label: string; source: 'label_values' | 'loki_label_values' | 'custom' | 'postgres_instances' | 'slos';
    label_name: string; match: string; options: string; default: string; multi: boolean };

const varOf = (c: DashboardControl): VarDraft => ({ id: c.id, label: c.label, source: c.query?.source ?? 'custom', label_name: c.query?.label ?? '',
    match: c.query?.match ?? '', options: (c.options ?? []).join(', '), default: (c.default as string) ?? '', multi: !!c.multi });

const controlOf = (v: VarDraft): DashboardControl => ({ id: v.id.trim(), label: v.label.trim() || v.id.trim(), kind: 'variable', column: '',
    default: v.default.trim() || null, multi: v.multi,
    ...(v.source === 'custom' ? { options: v.options.split(',').map((o) => o.trim()).filter(Boolean), query: null }
        : { options: null, query: { source: v.source, ...(v.label_name ? { label: v.label_name } : {}), ...(v.match ? { match: v.match } : {}) } }) });

/** A new ops dashboard, or an ops dashboard's tiles, variables, time range and refresh changed. Other tiles stay. */
export function OpsDashboardEditor({ open, board, onCancel, onSaved }: {
    open: boolean; board?: { id: string; name: string; description: string; shared: boolean; tiles: (OpsTile | Record<string, unknown>)[]; controls?: DashboardControl[] };
    onCancel: () => void; onSaved: (id: string) => void;
}) {
    const { client } = useConsumerApi();
    const time = board?.controls?.find((c) => c.kind === 'time');
    const [name, setName] = useState(board?.name ?? '');
    const [description, setDescription] = useState(board?.description ?? '');
    const [shared, setShared] = useState(board?.shared ?? false);
    const [range, setRange] = useState<string>((time?.default as string) ?? '1h');
    const [refresh, setRefresh] = useState<number>(time?.refresh ?? 30);
    const [vars, setVars] = useState<VarDraft[]>((board?.controls ?? []).filter((c) => c.kind === 'variable').map(varOf));
    const [tiles, setTiles] = useState<(OpsTile | Record<string, unknown>)[]>(board?.tiles ?? []);
    const [editing, setEditing] = useState<number | 'new' | null>(null);
    const [busy, setBusy] = useState(false);
    const others = (board?.controls ?? []).filter((c) => c.kind !== 'variable' && c.kind !== 'time');
    const controls: DashboardControl[] = [
        { id: 'time', label: 'Time range', kind: 'time', column: '', default: range, refresh },
        ...vars.filter((v) => /^[a-z0-9_-]{1,40}$/.test(v.id.trim()) && v.id.trim() !== 'time').map(controlOf), ...others];
    const values = Object.fromEntries(controls.map((c) => [c.id, c.default]));
    const setVar = (i: number, patch: Partial<VarDraft>) => setVars(vars.map((v, j) => (j === i ? { ...v, ...patch } : v)));
    const move = (i: number, by: number) => {
        const next = [...tiles];
        const [t] = next.splice(i, 1);
        next.splice(Math.max(0, Math.min(next.length, i + by)), 0, t);
        setTiles(next);
    };
    const save = async () => {
        setBusy(true);
        try {
            const body = { name: name.trim(), description, shared, tiles: tiles as never, controls };
            const saved = await client.saveDashboard(body, board?.id);
            onSaved(saved.id);
        } catch (e) {
            notifyError(e, { action: board ? 'Save the dashboard' : 'Create the ops dashboard' });
        } finally {
            setBusy(false);
        }
    };
    return (
        <Modal title={board ? `Edit ${board.name}` : 'New ops dashboard'} open={open} width={860} onCancel={onCancel}
            okText={board ? 'Save' : 'Create'} confirmLoading={busy} okButtonProps={{ disabled: !name.trim() || !tiles.length }} onOk={() => void save()}>
            <Space direction="vertical" style={{ width: '100%' }}>
                <Space wrap>
                    <Input aria-label="Dashboard name" placeholder="Name" value={name} style={{ width: 280 }} onChange={(e) => setName(e.target.value)} />
                    <Input aria-label="Description" placeholder="What it is for" value={description} style={{ width: 380 }} onChange={(e) => setDescription(e.target.value)} />
                </Space>
                <Space wrap>
                    <Muted>Default range</Muted>
                    <Select aria-label="Default range" value={range} style={{ width: 100 }} onChange={setRange} options={RANGES.map((r) => ({ value: r, label: r }))} />
                    <Muted>Refresh</Muted>
                    <Select aria-label="Default refresh" value={refresh} style={{ width: 100 }} onChange={setRefresh} options={REFRESHES} />
                </Space>
                <strong>Variables</strong>
                {vars.map((v, i) => (
                    <Space key={i} wrap>
                        <Input aria-label={`Variable ${i + 1} name`} addonBefore="$" placeholder="service" value={v.id} style={{ width: 150 }} onChange={(e) => setVar(i, { id: e.target.value })} />
                        <Input aria-label={`Variable ${i + 1} label`} placeholder="Label" value={v.label} style={{ width: 130 }} onChange={(e) => setVar(i, { label: e.target.value })} />
                        <Select aria-label={`Variable ${i + 1} choices`} value={v.source} style={{ width: 210 }} onChange={(source) => setVar(i, { source })}
                            options={[{ value: 'label_values', label: 'A Prometheus label’s values' }, { value: 'loki_label_values', label: 'A Loki label’s values' },
                                { value: 'custom', label: 'A list' }, { value: 'postgres_instances', label: 'PostgreSQL instances' }, { value: 'slos', label: 'SLOs' }]} />
                        {(v.source === 'label_values' || v.source === 'loki_label_values') && (
                            <Input aria-label={`Variable ${i + 1} label name`} placeholder="label, e.g. service_name" value={v.label_name} style={{ width: 170 }}
                                onChange={(e) => setVar(i, { label_name: e.target.value })} />
                        )}
                        {v.source === 'label_values' && (
                            <Input aria-label={`Variable ${i + 1} series`} placeholder="in series (optional)" value={v.match} style={{ width: 200 }}
                                onChange={(e) => setVar(i, { match: e.target.value })} />
                        )}
                        {v.source === 'custom' && (
                            <Input aria-label={`Variable ${i + 1} options`} placeholder="a, b, c" value={v.options} style={{ width: 200 }} onChange={(e) => setVar(i, { options: e.target.value })} />
                        )}
                        <Input aria-label={`Variable ${i + 1} default`} placeholder="default" value={v.default} style={{ width: 130 }} onChange={(e) => setVar(i, { default: e.target.value })} />
                        <Checkbox checked={v.multi} onChange={(e) => setVar(i, { multi: e.target.checked })}>several</Checkbox>
                        <Button aria-label={`Remove variable ${i + 1}`} onClick={() => setVars(vars.filter((_, j) => j !== i))}>×</Button>
                    </Space>
                ))}
                <Button size="small" onClick={() => setVars([...vars, { id: '', label: '', source: 'label_values', label_name: 'service_name', match: '', options: '', default: '', multi: false }])}>
                    Add variable
                </Button>
                <strong>Tiles</strong>
                <ol aria-label="Tiles" style={{ paddingLeft: 20, margin: 0 }}>
                    {tiles.map((t, i) => {
                        const src = sourceOf(t as OpsTile);
                        return (
                            <li key={i} style={{ marginBottom: 4 }}>
                                <Space wrap>
                                    <span>{(t as OpsTile).title || `Tile ${i + 1}`}</span>
                                    <Tag>{src ? OPS_SOURCES.find((s) => s.value === src)!.label : 'SQL, metric or workbook (kept)'}</Tag>
                                    {src && <Button size="small" onClick={() => setEditing(i)} aria-label={`Edit ${(t as OpsTile).title || `tile ${i + 1}`}`}>Edit</Button>}
                                    <Button size="small" disabled={i === 0} onClick={() => move(i, -1)} aria-label={`Move tile ${i + 1} up`}>↑</Button>
                                    <Button size="small" disabled={i === tiles.length - 1} onClick={() => move(i, 1)} aria-label={`Move tile ${i + 1} down`}>↓</Button>
                                    <Button size="small" danger onClick={() => setTiles(tiles.filter((_, j) => j !== i))} aria-label={`Remove tile ${i + 1}`}>Remove</Button>
                                </Space>
                            </li>
                        );
                    })}
                </ol>
                <Button onClick={() => setEditing('new')} disabled={tiles.length >= 24}>Add tile</Button>
                <Checkbox checked={shared} onChange={(e) => setShared(e.target.checked)}>
                    Share with everyone (only people with VIEW_OPERATIONS see the ops tiles&apos; data)
                </Checkbox>
            </Space>
            {editing !== null && (
                <OpsTileEditor open initial={editing === 'new' ? undefined : (tiles[editing] as OpsTile)} controls={controls} values={values}
                    onCancel={() => setEditing(null)}
                    onSave={(t) => {
                        setTiles(editing === 'new' ? [...tiles, t] : tiles.map((x, j) => (j === editing ? t : x)));
                        setEditing(null);
                    }} />
            )}
        </Modal>
    );
}
