import React from 'react';
import { render } from '@testing-library/react';

import type { Evaluation } from '../api/types';
import {
    EvaluationChart,
    buildBandPath,
    buildLinePath,
    computeXDomain,
    computeYDomain,
    linearScale,
    toChartPoints,
} from '../quality/EvaluationChart';

function ev(i: number, value: number | null, status: Evaluation['status'] = 'PASS', extra: Partial<Evaluation> = {}): Evaluation {
    return {
        id: `e${i}`,
        monitor_id: 'm1',
        ts: new Date(Date.UTC(2026, 0, 1, i)).toISOString(),
        metric_value: value,
        expected_low: 90,
        expected_high: 110,
        status,
        score: null,
        message: '',
        feedback: null,
        ...extra,
    };
}

describe('EvaluationChart helpers', () => {
    it('sorts points chronologically and flags anomalies unless marked expected', () => {
        const pts = toChartPoints([ev(3, 300, 'FAIL'), ev(1, 100), ev(2, 50, 'WARN', { feedback: 'EXPECTED' })]);
        expect(pts.map((p) => p.evaluation.id)).toEqual(['e1', 'e2', 'e3']);
        expect(pts.map((p) => p.anomaly)).toEqual([false, false, true]);
    });

    it('computes padded y domain including the expected band', () => {
        const d = computeYDomain(toChartPoints([ev(1, 100), ev(2, 200)]));
        // values 90..200 → span 110, pad 5.5
        expect(d.min).toBeCloseTo(84.5);
        expect(d.max).toBeCloseTo(205.5);
    });

    it('widens degenerate domains', () => {
        const pts = toChartPoints([ev(1, 5, 'PASS', { expected_low: null, expected_high: null })]);
        expect(computeYDomain(pts)).toEqual({ min: 4.5, max: 5.5 });
        expect(computeYDomain([])).toEqual({ min: 0, max: 1 });
        const x = computeXDomain(pts);
        expect(x.max - x.min).toBe(2);
    });

    it('linearScale maps domain endpoints onto the (possibly inverted) range', () => {
        const y = linearScale({ min: 0, max: 100 }, 200, 0);
        expect(y(0)).toBe(200);
        expect(y(100)).toBe(0);
        expect(y(50)).toBe(100);
    });

    it('breaks the line on null values and builds one band polygon per contiguous run', () => {
        const pts = toChartPoints([
            ev(1, 100),
            ev(2, null),
            ev(3, 105),
            ev(4, 101, 'PASS', { expected_low: null, expected_high: null }),
            ev(5, 99),
        ]);
        const id = (v: number) => v;
        const line = buildLinePath(pts, (p) => p.value, id, id);
        expect((line.match(/M/g) ?? []).length).toBe(2);
        const band = buildBandPath(pts, id, id);
        expect((band.match(/Z/g) ?? []).length).toBe(2);
    });
});

describe('<EvaluationChart />', () => {
    it('renders anomaly markers and an accessible label', () => {
        const { getAllByTestId, getByRole } = render(
            <EvaluationChart evaluations={[ev(1, 100), ev(2, 400, 'FAIL'), ev(3, 101)]} />,
        );
        expect(getAllByTestId('anomaly')).toHaveLength(1);
        expect(getAllByTestId('point')).toHaveLength(2);
        expect(getByRole('img').getAttribute('aria-label')).toContain('3 evaluations, 1 anomalies');
    });

    it('renders an empty state', () => {
        const { getByText } = render(<EvaluationChart evaluations={[]} />);
        expect(getByText('No evaluations yet')).toBeTruthy();
    });
});
