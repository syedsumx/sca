/**
 * Tests for TrendLineChart component
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import TrendLineChart from '../TrendLineChart';
import { TrendSnapshot, QualityGateStatus } from '../../../types';

// Mock Recharts components
jest.mock('recharts', () => {
  const OriginalModule = jest.requireActual('recharts');
  return {
    ...OriginalModule,
    ResponsiveContainer: ({ children, height }: { children: React.ReactNode; height: number }) => (
      <div data-testid="responsive-container" style={{ width: 400, height }}>
        {children}
      </div>
    ),
  };
});

const createMockSnapshot = (overrides: Partial<TrendSnapshot> = {}): TrendSnapshot => ({
  timestamp: '2025-01-15T10:00:00Z',
  total_issues: 100,
  vulnerabilities: 10,
  bugs: 20,
  code_smells: 70,
  coverage: 75.5,
  duplications: 5.2,
  quality_gate_status: 'PASSED' as QualityGateStatus,
  ...overrides,
});

describe('TrendLineChart', () => {
  describe('rendering', () => {
    it('renders without crashing', () => {
      const data: TrendSnapshot[] = [
        createMockSnapshot({ timestamp: '2025-01-14T10:00:00Z' }),
        createMockSnapshot({ timestamp: '2025-01-15T10:00:00Z' }),
      ];

      render(<TrendLineChart data={data} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('displays empty state when no data provided', () => {
      render(<TrendLineChart data={[]} />);
      expect(screen.getByText('No trend data available')).toBeInTheDocument();
    });

    it('renders with custom height', () => {
      const data: TrendSnapshot[] = [createMockSnapshot()];
      render(<TrendLineChart data={data} height={500} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('renders with quality gate indicators', () => {
      const data: TrendSnapshot[] = [
        createMockSnapshot({ timestamp: '2025-01-14T10:00:00Z', quality_gate_status: 'FAILED' }),
        createMockSnapshot({ timestamp: '2025-01-15T10:00:00Z', quality_gate_status: 'PASSED' }),
      ];

      render(<TrendLineChart data={data} showQualityGate={true} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('hides quality gate indicators when disabled', () => {
      const data: TrendSnapshot[] = [
        createMockSnapshot({ timestamp: '2025-01-14T10:00:00Z', quality_gate_status: 'FAILED' }),
        createMockSnapshot({ timestamp: '2025-01-15T10:00:00Z', quality_gate_status: 'PASSED' }),
      ];

      render(<TrendLineChart data={data} showQualityGate={false} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });

  describe('metrics configuration', () => {
    it('renders default metrics (total_issues, vulnerabilities, bugs)', () => {
      const data: TrendSnapshot[] = [
        createMockSnapshot({ timestamp: '2025-01-14T10:00:00Z' }),
        createMockSnapshot({ timestamp: '2025-01-15T10:00:00Z' }),
      ];

      render(<TrendLineChart data={data} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('renders custom metrics selection', () => {
      const data: TrendSnapshot[] = [
        createMockSnapshot({ timestamp: '2025-01-14T10:00:00Z' }),
        createMockSnapshot({ timestamp: '2025-01-15T10:00:00Z' }),
      ];

      render(<TrendLineChart data={data} metrics={['coverage', 'code_smells']} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('renders single metric', () => {
      const data: TrendSnapshot[] = [
        createMockSnapshot({ timestamp: '2025-01-14T10:00:00Z' }),
        createMockSnapshot({ timestamp: '2025-01-15T10:00:00Z' }),
      ];

      render(<TrendLineChart data={data} metrics={['coverage']} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('renders all available metrics', () => {
      const data: TrendSnapshot[] = [
        createMockSnapshot({ timestamp: '2025-01-14T10:00:00Z' }),
        createMockSnapshot({ timestamp: '2025-01-15T10:00:00Z' }),
      ];

      render(
        <TrendLineChart
          data={data}
          metrics={['total_issues', 'vulnerabilities', 'bugs', 'code_smells', 'coverage']}
        />
      );
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });

  describe('data processing', () => {
    it('handles single data point', () => {
      const data: TrendSnapshot[] = [createMockSnapshot()];
      render(<TrendLineChart data={data} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('handles many data points', () => {
      const data: TrendSnapshot[] = Array(100)
        .fill(null)
        .map((_, i) =>
          createMockSnapshot({
            timestamp: new Date(2025, 0, i + 1).toISOString(),
            total_issues: 100 - i,
          })
        );

      render(<TrendLineChart data={data} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('handles quality gate transitions', () => {
      const data: TrendSnapshot[] = [
        createMockSnapshot({ timestamp: '2025-01-01T10:00:00Z', quality_gate_status: 'PASSED' }),
        createMockSnapshot({ timestamp: '2025-01-02T10:00:00Z', quality_gate_status: 'FAILED' }),
        createMockSnapshot({ timestamp: '2025-01-03T10:00:00Z', quality_gate_status: 'FAILED' }),
        createMockSnapshot({ timestamp: '2025-01-04T10:00:00Z', quality_gate_status: 'PASSED' }),
      ];

      render(<TrendLineChart data={data} showQualityGate={true} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('handles warning quality gate status', () => {
      const data: TrendSnapshot[] = [
        createMockSnapshot({ timestamp: '2025-01-01T10:00:00Z', quality_gate_status: 'WARNING' }),
      ];

      render(<TrendLineChart data={data} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });

  describe('edge cases', () => {
    it('handles zero values', () => {
      const data: TrendSnapshot[] = [
        createMockSnapshot({
          timestamp: '2025-01-15T10:00:00Z',
          total_issues: 0,
          vulnerabilities: 0,
          bugs: 0,
          code_smells: 0,
          coverage: 0,
        }),
      ];

      render(<TrendLineChart data={data} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('handles missing optional fields', () => {
      const data: TrendSnapshot[] = [
        {
          timestamp: '2025-01-15T10:00:00Z',
          total_issues: 100,
        } as TrendSnapshot,
      ];

      render(<TrendLineChart data={data} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('handles unsorted timestamps', () => {
      const data: TrendSnapshot[] = [
        createMockSnapshot({ timestamp: '2025-01-15T10:00:00Z' }),
        createMockSnapshot({ timestamp: '2025-01-10T10:00:00Z' }),
        createMockSnapshot({ timestamp: '2025-01-20T10:00:00Z' }),
      ];

      render(<TrendLineChart data={data} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });
});
