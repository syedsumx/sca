import React, { useMemo } from 'react';
import styled from 'styled-components';

import type { Evaluation } from '../api/types';
import { formatNumber, formatTimestamp } from '../common/format';

export interface ChartPoint {
    t: number;
    value: number | null;
    low: number | null;
    high: number | null;
    anomaly: boolean;
    evaluation: Evaluation;
}

export interface Domain {
    min: number;
    max: number;
}

/** Sorts evaluations chronologically and converts them to plottable points. */
export function toChartPoints(evaluations: Evaluation[]): ChartPoint[] {
    return evaluations
        .map((e) => ({
            t: new Date(e.ts).getTime(),
            value: e.metric_value,
            low: e.expected_low,
            high: e.expected_high,
            anomaly: (e.status === 'FAIL' || e.status === 'WARN') && e.feedback !== 'EXPECTED' && e.feedback !== 'NEW_NORMAL',
            evaluation: e,
        }))
        .filter((p) => !Number.isNaN(p.t))
        .sort((a, b) => a.t - b.t);
}

/** Y domain covering values and expected band, padded by 5%. Degenerate ranges are widened. */
export function computeYDomain(points: ChartPoint[]): Domain {
    const vals: number[] = [];
    points.forEach((p) => {
        [p.value, p.low, p.high].forEach((v) => {
            if (v !== null && Number.isFinite(v)) vals.push(v);
        });
    });
    if (vals.length === 0) return { min: 0, max: 1 };
    let min = Math.min(...vals);
    let max = Math.max(...vals);
    if (min === max) {
        const pad = Math.abs(min) * 0.1 || 1;
        return { min: min - pad, max: max + pad };
    }
    const pad = (max - min) * 0.05;
    min -= pad;
    max += pad;
    return { min, max };
}

export function computeXDomain(points: ChartPoint[]): Domain {
    if (points.length === 0) return { min: 0, max: 1 };
    const min = points[0].t;
    const max = points[points.length - 1].t;
    return min === max ? { min: min - 1, max: max + 1 } : { min, max };
}

/** Linear scale mapping [domain.min, domain.max] → [rangeStart, rangeEnd]. */
export function linearScale(domain: Domain, rangeStart: number, rangeEnd: number) {
    const span = domain.max - domain.min || 1;
    return (v: number) => rangeStart + ((v - domain.min) / span) * (rangeEnd - rangeStart);
}

/**
 * Builds an SVG path for a series, starting a new sub-path ("M") after any gap (null value), so
 * missing evaluations are not interpolated across.
 */
export function buildLinePath(
    points: ChartPoint[],
    accessor: (p: ChartPoint) => number | null,
    x: (t: number) => number,
    y: (v: number) => number,
): string {
    let path = '';
    let penDown = false;
    points.forEach((p) => {
        const v = accessor(p);
        if (v === null || !Number.isFinite(v)) {
            penDown = false;
            return;
        }
        path += `${penDown ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(v).toFixed(1)}`;
        penDown = true;
    });
    return path;
}

/** Closed polygon for the expected band over contiguous runs that have both bounds. */
export function buildBandPath(
    points: ChartPoint[],
    x: (t: number) => number,
    y: (v: number) => number,
): string {
    const segments: ChartPoint[][] = [];
    let current: ChartPoint[] = [];
    points.forEach((p) => {
        if (p.low !== null && p.high !== null && Number.isFinite(p.low) && Number.isFinite(p.high)) {
            current.push(p);
        } else if (current.length) {
            segments.push(current);
            current = [];
        }
    });
    if (current.length) segments.push(current);
    return segments
        .map((seg) => {
            const top = seg.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(p.t).toFixed(1)},${y(p.high as number).toFixed(1)}`);
            const bottom = [...seg]
                .reverse()
                .map((p) => `L${x(p.t).toFixed(1)},${y(p.low as number).toFixed(1)}`);
            return `${top.join('')}${bottom.join('')}Z`;
        })
        .join('');
}

const Figure = styled.figure`
    margin: 0;
    width: 100%;

    svg {
        width: 100%;
        height: auto;
        display: block;
        overflow: visible;
    }

    figcaption {
        font-size: 12px;
        color: #8c8c8c;
        margin-top: 4px;
    }
`;

export interface EvaluationChartProps {
    evaluations: Evaluation[];
    width?: number;
    height?: number;
    title?: string;
    onSelect?: (evaluation: Evaluation) => void;
}

const MARGIN = { top: 12, right: 12, bottom: 24, left: 56 };

export function EvaluationChart({
    evaluations,
    width = 720,
    height = 220,
    title = 'Metric history',
    onSelect,
}: EvaluationChartProps) {
    const points = useMemo(() => toChartPoints(evaluations), [evaluations]);
    const geometry = useMemo(() => {
        const xd = computeXDomain(points);
        const yd = computeYDomain(points);
        const x = linearScale(xd, MARGIN.left, width - MARGIN.right);
        const y = linearScale(yd, height - MARGIN.bottom, MARGIN.top);
        const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => yd.min + f * (yd.max - yd.min));
        return {
            x,
            y,
            xd,
            ticks,
            line: buildLinePath(points, (p) => p.value, x, y),
            band: buildBandPath(points, x, y),
        };
    }, [points, width, height]);

    if (points.length === 0) {
        return (
            <Figure>
                <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${title}: no data`}>
                    <text x={width / 2} y={height / 2} textAnchor="middle" fill="#8c8c8c" fontSize={13}>
                        No evaluations yet
                    </text>
                </svg>
            </Figure>
        );
    }

    const anomalies = points.filter((p) => p.anomaly && p.value !== null);
    const { x, y, xd, ticks, line, band } = geometry;

    return (
        <Figure>
            <svg
                viewBox={`0 0 ${width} ${height}`}
                role="img"
                aria-label={`${title}: ${points.length} evaluations, ${anomalies.length} anomalies`}
            >
                {ticks.map((t) => (
                    <g key={t}>
                        <line x1={MARGIN.left} x2={width - MARGIN.right} y1={y(t)} y2={y(t)} stroke="#f0f0f0" />
                        <text x={MARGIN.left - 6} y={y(t) + 4} textAnchor="end" fontSize={10} fill="#8c8c8c">
                            {formatNumber(t)}
                        </text>
                    </g>
                ))}
                <text x={MARGIN.left} y={height - 6} fontSize={10} fill="#8c8c8c">
                    {formatTimestamp(new Date(xd.min).toISOString())}
                </text>
                <text x={width - MARGIN.right} y={height - 6} fontSize={10} fill="#8c8c8c" textAnchor="end">
                    {formatTimestamp(new Date(xd.max).toISOString())}
                </text>
                {band && <path d={band} fill="#1890ff" fillOpacity={0.12} stroke="none" data-testid="band" />}
                <path d={line} fill="none" stroke="#1890ff" strokeWidth={1.75} data-testid="line" />
                {points
                    .filter((p) => p.value !== null)
                    .map((p) => (
                        <circle
                            key={p.evaluation.id}
                            cx={x(p.t)}
                            cy={y(p.value as number)}
                            r={p.anomaly ? 4.5 : 2.5}
                            fill={p.anomaly ? (p.evaluation.status === 'FAIL' ? '#f5222d' : '#faad14') : '#1890ff'}
                            stroke="#fff"
                            strokeWidth={1}
                            style={{ cursor: onSelect ? 'pointer' : undefined }}
                            onClick={onSelect ? () => onSelect(p.evaluation) : undefined}
                            data-testid={p.anomaly ? 'anomaly' : 'point'}
                        >
                            <title>
                                {`${formatTimestamp(p.evaluation.ts)} — ${formatNumber(p.value)} (${p.evaluation.status})${
                                    p.low !== null && p.high !== null
                                        ? ` expected ${formatNumber(p.low)}–${formatNumber(p.high)}`
                                        : ''
                                }`}
                            </title>
                        </circle>
                    ))}
            </svg>
            <figcaption>
                Line: observed metric · shaded band: expected range · red/amber markers: anomalies
            </figcaption>
        </Figure>
    );
}

export default EvaluationChart;
