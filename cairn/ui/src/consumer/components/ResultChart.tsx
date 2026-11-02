import React from 'react';

import type { ChartSpec } from '../api/types';

const COLORS = ['#3b5bdb', '#2f9e44', '#e8590c', '#ae3ec9', '#1098ad', '#f08c00', '#c2255c', '#5c940d'];

/** Bars or lines of numeric columns ``y`` against column ``x`` of a query result (plain SVG). */
export function ResultChart({
    columns,
    rows,
    chart,
    height = 260,
}: {
    columns: string[];
    rows: unknown[][];
    chart: ChartSpec;
    height?: number;
}) {
    const xi = columns.indexOf(chart.x ?? '');
    const ys = (chart.y ?? []).map((c) => columns.indexOf(c)).filter((i) => i >= 0);
    if (chart.type === 'table' || xi < 0 || !ys.length || !rows.length)
        return <div style={{ color: '#888', padding: 12 }}>Pick a column for x and at least one numeric column for y.</div>;
    const data = rows.slice(0, 200);
    const values = data.flatMap((r) => ys.map((i) => Number(r[i]) || 0));
    const max = Math.max(0, ...values);
    const min = Math.min(0, ...values);
    const width = 720;
    const pad = { l: 56, r: 12, t: 12, b: 44 };
    const w = width - pad.l - pad.r;
    const h = height - pad.t - pad.b;
    const yScale = (v: number) => pad.t + h - ((v - min) / (max - min || 1)) * h;
    const step = w / data.length;
    return (
        <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${chart.type} chart of ${chart.y?.join(', ')} by ${chart.x}`}
            style={{ width: '100%', maxWidth: width }}>
            <line x1={pad.l} y1={yScale(0)} x2={width - pad.r} y2={yScale(0)} stroke="#ccc" />
            {[max, (max + min) / 2, min].map((v) => (
                <text key={v} x={pad.l - 6} y={yScale(v) + 4} fontSize="11" textAnchor="end" fill="#666">
                    {Number.isInteger(v) ? v : v.toFixed(2)}
                </text>
            ))}
            {ys.map((col, k) =>
                chart.type === 'bar' ? (
                    data.map((r, j) => {
                        const v = Number(r[col]) || 0;
                        const bw = Math.max(1, (step * 0.8) / ys.length);
                        const x = pad.l + j * step + step * 0.1 + k * bw;
                        return (
                            <rect key={`${k}-${j}`} x={x} width={bw} y={Math.min(yScale(v), yScale(0))}
                                height={Math.abs(yScale(v) - yScale(0))} fill={COLORS[k % COLORS.length]}>
                                <title>{`${String(r[xi])}: ${v}`}</title>
                            </rect>
                        );
                    })
                ) : (
                    <polyline key={k} fill="none" stroke={COLORS[k % COLORS.length]} strokeWidth={2}
                        points={data.map((r, j) => `${pad.l + j * step + step / 2},${yScale(Number(r[col]) || 0)}`).join(' ')} />
                ),
            )}
            {data.map((r, j) =>
                data.length <= 24 || j % Math.ceil(data.length / 24) === 0 ? (
                    <text key={j} x={pad.l + j * step + step / 2} y={height - pad.b + 14} fontSize="10" textAnchor="middle" fill="#666">
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
