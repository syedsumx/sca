import { Grid } from 'antd';
import React from 'react';

import type { ChartSpec } from '../api/types';
import { useDheSession } from '../context/CairnProvider';

const COLORS = ['#3b5bdb', '#2f9e44', '#e8590c', '#ae3ec9', '#1098ad', '#f08c00', '#c2255c', '#5c940d'];

const fmt = (v: number) => (Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 2 }));

/** A query result as a chart (plain SVG): bars (side by side or stacked), lines, areas, points, a pie or one
 * big number, of numeric columns ``y`` against column ``x``; a heatmap of ``y`` by ``x`` and ``series``; or a map
 * of rows at ``lat``/``lon``, sized by ``y``. ``onSelect`` is called with the x value clicked
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
    const tiles = useDheSession().config;
    // Charts are drawn in a box this wide and scaled to fit: narrower on a phone, so their text stays readable.
    const width = Grid.useBreakpoint().md === false ? 400 : 720;
    if (chart.type === 'heatmap')
        return <Heatmap columns={columns} rows={rows} chart={chart} height={height} width={width} onSelect={onSelect} selected={selected} />;
    if (chart.type === 'map')
        return <PointMap columns={columns} rows={rows} chart={chart} height={height} width={width} onSelect={onSelect} selected={selected}
            tiles={tiles?.map_tiles ?? null} attribution={tiles?.map_attribution ?? null} />;
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

interface Drawn {
    columns: string[];
    rows: unknown[][];
    chart: ChartSpec;
    height: number;
    width: number;
    onSelect?: (x: unknown) => void;
    selected?: unknown;
}

const hint = (text: string) => <div style={{ color: '#888', padding: 12 }}>{text}</div>;

/** Light to dark blue, for 0..1. */
function shade(t: number): string {
    const a = [231, 245, 255], b = [24, 100, 171];
    const c = a.map((v, i) => Math.round(v + (b[i] - v) * Math.min(1, Math.max(0, t))));
    return `rgb(${c[0]},${c[1]},${c[2]})`;
}

/** One value coloured by two columns: x across, ``series`` down. */
function Heatmap({ columns, rows, chart, height, width, onSelect, selected }: Drawn) {
    const xi = columns.indexOf(chart.x ?? ''), si = columns.indexOf(chart.series ?? ''), vi = columns.indexOf(chart.y?.[0] ?? '');
    if (xi < 0 || si < 0 || vi < 0 || !rows.length) return hint('Pick a column across (x), one down (series) and a numeric value (y).');
    const key = (v: unknown) => String(v ?? '');
    const xs = [...new Set(rows.map((r) => key(r[xi])))].slice(0, 60);
    const ss = [...new Set(rows.map((r) => key(r[si])))].slice(0, 40);
    const cell = new Map<string, number>();
    rows.forEach((r) => {
        const k = `${key(r[xi])}\u0000${key(r[si])}`;
        cell.set(k, (cell.get(k) ?? 0) + (Number(r[vi]) || 0));
    });
    const vals = [...cell.values()];
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = { l: width < 720 ? 64 : 96, r: 8, t: 8, b: 40 };
    const h = Math.max(height, pad.t + pad.b + ss.length * 14);
    const cw = (width - pad.l - pad.r) / xs.length, ch = (h - pad.t - pad.b) / ss.length;
    const style = onSelect ? { cursor: 'pointer' } : undefined;
    return (
        <svg viewBox={`0 0 ${width} ${h}`} role="img" aria-label={`heatmap of ${columns[vi]} by ${columns[xi]} and ${columns[si]}`}
            style={{ width: '100%', maxWidth: width }}>
            {ss.map((sv, j) => (
                <text key={`s${j}`} x={pad.l - 6} y={pad.t + j * ch + ch / 2 + 4} fontSize="11" textAnchor="end" fill="#666">{sv.slice(0, 14)}</text>
            ))}
            {xs.map((xv, i) => ss.map((sv, j) => {
                const v = cell.get(`${xv}\u0000${sv}`);
                const dim = selected !== undefined && key(selected) !== xv;
                return (
                    <rect key={`${i}-${j}`} x={pad.l + i * cw} y={pad.t + j * ch} width={Math.max(1, cw - 1)} height={Math.max(1, ch - 1)}
                        fill={v === undefined ? '#f6f8fa' : shade(hi > lo ? (v - lo) / (hi - lo) : 1)} opacity={dim ? 0.4 : 1}
                        onClick={onSelect ? () => onSelect(rows.find((r) => key(r[xi]) === xv)?.[xi]) : undefined} style={style}>
                        <title>{`${xv} · ${sv}: ${v === undefined ? 'none' : fmt(v)}`}</title>
                    </rect>
                );
            }))}
            {xs.map((xv, i) => (xs.length <= 24 || i % Math.ceil(xs.length / 24) === 0) && (
                <text key={`x${i}`} x={pad.l + i * cw + cw / 2} y={h - pad.b + 14} fontSize="10" textAnchor="middle" fill="#666">{xv.slice(0, 10)}</text>
            ))}
            <text x={pad.l} y={h - 6} fontSize="11" fill="#666">{columns[vi]}: {fmt(lo)}</text>
            {[0, 0.25, 0.5, 0.75, 1].map((t, k) => <rect key={`k${k}`} x={pad.l + 120 + k * 16} y={h - 16} width={16} height={10} fill={shade(t)} />)}
            <text x={pad.l + 210} y={h - 6} fontSize="11" fill="#666">{fmt(hi)}</text>
        </svg>
    );
}

const TILE = 256;
/** Web Mercator, in world units 0..1. */
const project = (lat: number, lon: number): [number, number] => {
    const l = (Math.max(-85.0511, Math.min(85.0511, lat)) * Math.PI) / 180;
    return [(lon + 180) / 360, (1 - Math.log(Math.tan(l) + 1 / Math.cos(l)) / Math.PI) / 2];
};

/** Rows placed at their latitude and longitude (Web Mercator), sized by y, on the operator's tiles if any. */
function PointMap({ columns, rows, chart, height, width, onSelect, selected, tiles, attribution }: Drawn & {
    tiles: string | null; attribution: string | null;
}) {
    const la = columns.indexOf(chart.lat ?? ''), lo = columns.indexOf(chart.lon ?? '');
    const xi = columns.indexOf(chart.x ?? ''), vi = columns.indexOf(chart.y?.[0] ?? '');
    if (la < 0 || lo < 0) return hint('Pick the latitude and longitude columns.');
    const pts = rows.slice(0, 2000).map((r) => ({ r, lat: Number(r[la]), lon: Number(r[lo]) }))
        .filter((p) => p.r[la] !== null && p.r[lo] !== null && Number.isFinite(p.lat) && Number.isFinite(p.lon)
            && Math.abs(p.lat) <= 90 && Math.abs(p.lon) <= 180);
    if (!pts.length) return hint('No rows with a latitude and longitude to show.');
    const w = pts.map((p) => project(p.lat, p.lon));
    const [x0, x1] = [Math.min(...w.map((p) => p[0])), Math.max(...w.map((p) => p[0]))];
    const [y0, y1] = [Math.min(...w.map((p) => p[1])), Math.max(...w.map((p) => p[1]))];
    // The zoom that fits every point with a margin; whole zoom levels, so tiles line up.
    const fit = Math.min((width * 0.85) / Math.max(x1 - x0, 1e-9), (height * 0.8) / Math.max(y1 - y0, 1e-9));
    const z = Math.max(0, Math.min(16, Math.floor(Math.log2(fit / TILE))));
    const scale = TILE * 2 ** z;
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    const px = (wx: number) => (wx - cx) * scale + width / 2, py = (wy: number) => (wy - cy) * scale + height / 2;
    const vals = vi >= 0 ? pts.map((p) => Math.max(0, Number(p.r[vi]) || 0)) : [];
    const top = Math.max(0, ...vals) || 1;
    const radius = (k: number) => (vi >= 0 ? 3 + 15 * Math.sqrt(vals[k] / top) : 5);
    const images: React.ReactNode[] = [];
    if (tiles) {
        const n = 2 ** z;
        const tx0 = Math.floor((cx * scale - width / 2) / TILE), tx1 = Math.floor((cx * scale + width / 2) / TILE);
        const ty0 = Math.max(0, Math.floor((cy * scale - height / 2) / TILE)), ty1 = Math.min(n - 1, Math.floor((cy * scale + height / 2) / TILE));
        for (let tx = tx0; tx <= tx1; tx++)
            for (let ty = ty0; ty <= ty1; ty++) {
                const wrapped = ((tx % n) + n) % n;
                const href = tiles.replace('{z}', String(z)).replace('{x}', String(wrapped)).replace('{y}', String(ty));
                images.push(<image key={`${tx}/${ty}`} href={href} x={px((tx * TILE) / scale)} y={py((ty * TILE) / scale)}
                    width={TILE + 0.5} height={TILE + 0.5} preserveAspectRatio="none" />);
            }
    }
    const style = onSelect ? { cursor: 'pointer' } : undefined;
    const grid = 2 ** Math.max(0, z - 1);
    return (
        <svg viewBox={`0 0 ${width} ${height}`} role="img" style={{ width: '100%', maxWidth: width, background: '#eef3f8' }}
            aria-label={`map of ${pts.length} point${pts.length === 1 ? '' : 's'}${vi >= 0 ? ` sized by ${columns[vi]}` : ''}`}>
            {images}
            {!tiles && [...Array(9)].map((_, k) => {
                // Graticule lines, so a map without tiles still reads as one.
                const gx = Math.floor(cx * grid) / grid + (k - 4) / grid, gy = Math.floor(cy * grid) / grid + (k - 4) / grid;
                return (
                    <g key={`g${k}`} stroke="#d0d7de" strokeWidth={1}>
                        <line x1={px(gx)} x2={px(gx)} y1={0} y2={height} />
                        <line y1={py(gy)} y2={py(gy)} x1={0} x2={width} />
                    </g>
                );
            })}
            {pts.map((p, k) => {
                const name = xi >= 0 ? String(p.r[xi] ?? '') : `${p.lat}, ${p.lon}`;
                const sel = selected !== undefined && xi >= 0 && p.r[xi] === selected;
                return (
                    <circle key={k} cx={px(w[k][0])} cy={py(w[k][1])} r={radius(k)} fill={COLORS[0]} fillOpacity={0.6}
                        stroke={sel ? '#e8590c' : '#fff'} strokeWidth={sel ? 3 : 1}
                        opacity={selected !== undefined && !sel ? 0.45 : 1}
                        onClick={onSelect && xi >= 0 ? () => onSelect(p.r[xi]) : undefined} style={style}>
                        <title>{vi >= 0 ? `${name}: ${fmt(Number(p.r[vi]) || 0)}` : name}</title>
                    </circle>
                );
            })}
            {xi >= 0 && pts.length <= 20 && pts.map((p, k) => (
                // A few points are named on the map itself: a phone has no hover to show their titles.
                <text key={`n${k}`} x={px(w[k][0]) + radius(k) + 3} y={py(w[k][1]) + 4} fontSize="11" fill="#1f2328"
                    stroke="#fff" strokeWidth={3} paintOrder="stroke" pointerEvents="none">{String(p.r[xi] ?? '').slice(0, 24)}</text>
            ))}
            {tiles && attribution && (
                <text x={width - 4} y={height - 4} fontSize="9" textAnchor="end" fill="#333" stroke="#fff" strokeWidth={2}
                    paintOrder="stroke">{attribution}</text>
            )}
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
