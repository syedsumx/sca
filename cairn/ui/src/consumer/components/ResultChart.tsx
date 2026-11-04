import React from 'react';

import type { ChartSpec } from '../api/types';

const COLORS = ['#3b5bdb', '#2f9e44', '#e8590c', '#ae3ec9', '#1098ad', '#f08c00', '#c2255c', '#5c940d'];

const fmt = (v: number) => (Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 2 }));

/** A query result as a chart (plain SVG): bars (side by side or stacked), lines, areas, points, a pie or one
 * big number, of numeric columns ``y`` against column ``x``. ``onSelect`` is called with the x value clicked
 * (dashboards cross-filter and drill with it); ``selected`` is highlighted. */
export function ResultChart({
    columns,
    rows,
    chart,
    height = 260,
    onSelect,
    selected,
}: {
    columns: string[];
    rows: unknown[][];
    chart: ChartSpec;
    height?: number;
    onSelect?: (x: unknown) => void;
    selected?: unknown;
}) {
    const xi = columns.indexOf(chart.x ?? '');
    const ys = (chart.y ?? []).map((c) => columns.indexOf(c)).filter((i) => i >= 0);
    const label = `${chart.type} chart of ${chart.y?.join(', ')} by ${chart.x}`;
    if (chart.type === 'number') {
        const col = ys[0] ?? columns.findIndex((_, i) => typeof rows[0]?.[i] === 'number');
        if (col < 0 || !rows.length) return <div style={{ color: '#888', padding: 12 }}>No number to show.</div>;
        return (
            <div role="img" aria-label={`number: ${columns[col]}`} style={{ padding: '16px 8px' }}>
                <div style={{ fontSize: 40, fontWeight: 600, lineHeight: 1.1 }}>{fmt(Number(rows[0][col]) || 0)}</div>
                <div style={{ color: '#666' }}>{columns[col]}</div>
            </div>
        );
    }
    if (chart.type === 'table' || xi < 0 || !ys.length || !rows.length)
        return <div style={{ color: '#888', padding: 12 }}>Pick a column for x and at least one numeric column for y.</div>;
    const data = rows.slice(0, 200);
    const width = 720;
    const click = (r: unknown[]) => (onSelect ? () => onSelect(r[xi]) : undefined);
    const style = onSelect ? { cursor: 'pointer' } : undefined;
    const isSel = (r: unknown[]) => selected !== undefined && r[xi] === selected;

    if (chart.type === 'pie') {
        const col = ys[0];
        const total = data.reduce((s, r) => s + Math.max(0, Number(r[col]) || 0), 0) || 1;
        const cx = height / 2, cy = height / 2, rad = height / 2 - 10;
        let a0 = -Math.PI / 2;
        return (
            <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label} style={{ width: '100%', maxWidth: width }}>
                {data.map((r, j) => {
                    const v = Math.max(0, Number(r[col]) || 0);
                    const a1 = a0 + (v / total) * Math.PI * 2;
                    const large = a1 - a0 > Math.PI ? 1 : 0;
                    const d = v >= total
                        ? `M ${cx} ${cy - rad} A ${rad} ${rad} 0 1 1 ${cx - 0.01} ${cy - rad} Z`
                        : `M ${cx} ${cy} L ${cx + rad * Math.cos(a0)} ${cy + rad * Math.sin(a0)} A ${rad} ${rad} 0 ${large} 1 ${cx + rad * Math.cos(a1)} ${cy + rad * Math.sin(a1)} Z`;
                    a0 = a1;
                    return (
                        <path key={j} d={d} fill={COLORS[j % COLORS.length]} stroke="#fff" strokeWidth={isSel(r) ? 4 : 1}
                            opacity={selected !== undefined && !isSel(r) ? 0.45 : 1} onClick={click(r)} style={style}>
                            <title>{`${String(r[xi])}: ${fmt(v)}`}</title>
                        </path>
                    );
                })}
                {data.slice(0, 12).map((r, j) => (
                    <text key={`l${j}`} x={height + 24} y={20 + j * 18} fontSize="12" fill={COLORS[j % COLORS.length]}
                        onClick={click(r)} style={style}>
                        ■ {String(r[xi]).slice(0, 30)} ({Math.round(((Number(r[col]) || 0) / total) * 100)}%)
                    </text>
                ))}
            </svg>
        );
    }

    const stacked = chart.type === 'stacked_bar';
    const totals = data.map((r) => ys.reduce((s, i) => s + (Number(r[i]) || 0), 0));
    const values = stacked ? totals : data.flatMap((r) => ys.map((i) => Number(r[i]) || 0));
    const max = Math.max(0, ...values);
    const min = Math.min(0, ...values);
    const pad = { l: 56, r: 12, t: 12, b: 44 };
    const w = width - pad.l - pad.r;
    const h = height - pad.t - pad.b;
    const yScale = (v: number) => pad.t + h - ((v - min) / (max - min || 1)) * h;
    const step = w / data.length;
    const cxOf = (j: number) => pad.l + j * step + step / 2;
    return (
        <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label} style={{ width: '100%', maxWidth: width }}>
            <line x1={pad.l} y1={yScale(0)} x2={width - pad.r} y2={yScale(0)} stroke="#ccc" />
            {[max, (max + min) / 2, min].map((v) => (
                <text key={v} x={pad.l - 6} y={yScale(v) + 4} fontSize="11" textAnchor="end" fill="#666">
                    {Number.isInteger(v) ? v : v.toFixed(2)}
                </text>
            ))}
            {selected !== undefined && data.map((r, j) => isSel(r) && (
                <rect key={`s${j}`} x={pad.l + j * step} width={step} y={pad.t} height={h} fill="#fff3bf" />
            ))}
            {stacked
                ? data.map((r, j) => {
                    let base = 0;
                    return ys.map((col, k) => {
                        const v = Number(r[col]) || 0;
                        const y0 = yScale(base), y1 = yScale(base + v);
                        base += v;
                        return (
                            <rect key={`${k}-${j}`} x={pad.l + j * step + step * 0.1} width={Math.max(1, step * 0.8)}
                                y={Math.min(y0, y1)} height={Math.abs(y1 - y0)} fill={COLORS[k % COLORS.length]}
                                onClick={click(r)} style={style}>
                                <title>{`${String(r[xi])} · ${columns[col]}: ${fmt(v)}`}</title>
                            </rect>
                        );
                    });
                })
                : ys.map((col, k) => {
                    if (chart.type === 'bar') {
                        return data.map((r, j) => {
                            const v = Number(r[col]) || 0;
                            const bw = Math.max(1, (step * 0.8) / ys.length);
                            return (
                                <rect key={`${k}-${j}`} x={pad.l + j * step + step * 0.1 + k * bw} width={bw}
                                    y={Math.min(yScale(v), yScale(0))} height={Math.abs(yScale(v) - yScale(0))}
                                    fill={COLORS[k % COLORS.length]} onClick={click(r)} style={style}>
                                    <title>{`${String(r[xi])}: ${fmt(v)}`}</title>
                                </rect>
                            );
                        });
                    }
                    const pts = data.map((r, j) => `${cxOf(j)},${yScale(Number(r[col]) || 0)}`);
                    return (
                        <g key={k}>
                            {chart.type === 'area' && (
                                <polygon fill={COLORS[k % COLORS.length]} opacity={0.25}
                                    points={[`${cxOf(0)},${yScale(0)}`, ...pts, `${cxOf(data.length - 1)},${yScale(0)}`].join(' ')} />
                            )}
                            {chart.type !== 'scatter' && (
                                <polyline fill="none" stroke={COLORS[k % COLORS.length]} strokeWidth={2} points={pts.join(' ')} />
                            )}
                            {(chart.type === 'scatter' || onSelect) && data.map((r, j) => (
                                <circle key={j} cx={cxOf(j)} cy={yScale(Number(r[col]) || 0)} r={chart.type === 'scatter' ? 5 : 4}
                                    fill={COLORS[k % COLORS.length]} onClick={click(r)} style={style}>
                                    <title>{`${String(r[xi])}: ${fmt(Number(r[col]) || 0)}`}</title>
                                </circle>
                            ))}
                        </g>
                    );
                })}
            {data.map((r, j) =>
                data.length <= 24 || j % Math.ceil(data.length / 24) === 0 ? (
                    <text key={j} x={cxOf(j)} y={height - pad.b + 14} fontSize="10" textAnchor="middle" fill="#666"
                        onClick={click(r)} style={style}>
                        {String(r[xi]).slice(0, 10)}
                    </text>
                ) : null,
            )}
            {ys.map((col, k) => (
                <text key={`l${k}`} x={pad.l + k * 120} y={height - 6} fontSize="11" fill={COLORS[k % COLORS.length]}>
                    ■ {columns[col]}
                </text>
            ))}
        </svg>
    );
}

/** The result as CSV (RFC 4180 quoting). */
export function toCsv(columns: string[], rows: unknown[][]): string {
    const cell = (v: unknown) => {
        const s = v === null || v === undefined ? '' : String(v);
        return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    return [columns.map(cell).join(','), ...rows.map((r) => r.map(cell).join(','))].join('\r\n');
}
