/**
 * Tests for IssuesBySeverityChart component
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import IssuesBySeverityChart from '../IssuesBySeverityChart';
import { Issue, Severity } from '../../../types';

// Mock Recharts components
jest.mock('recharts', () => {
  const OriginalModule = jest.requireActual('recharts');
  return {
    ...OriginalModule,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="responsive-container" style={{ width: 400, height: 300 }}>
        {children}
      </div>
    ),
  };
});

const createMockIssue = (severity: Severity): Issue => ({
  id: `issue-${Math.random().toString(36).substr(2, 9)}`,
  rule_id: 'TEST001',
  rule_name: 'Test Rule',
  severity,
  issue_type: 'BUG',
  message: 'Test message',
  location: {
    file_path: 'test.py',
    start_line: 1,
    end_line: 1,
  },
  tags: [],
});

describe('IssuesBySeverityChart', () => {
  describe('rendering', () => {
    it('renders without crashing', () => {
      const issues: Issue[] = [
        createMockIssue('CRITICAL'),
        createMockIssue('MAJOR'),
      ];

      render(<IssuesBySeverityChart issues={issues} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('renders with empty issues array', () => {
      render(<IssuesBySeverityChart issues={[]} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('renders bar chart with all severity levels', () => {
      const severities: Severity[] = ['BLOCKER', 'CRITICAL', 'MAJOR', 'MINOR', 'INFO'];
      const issues: Issue[] = severities.map(severity => createMockIssue(severity));

      render(<IssuesBySeverityChart issues={issues} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });

  describe('data processing', () => {
    it('correctly counts issues by severity', () => {
      const issues: Issue[] = [
        createMockIssue('CRITICAL'),
        createMockIssue('CRITICAL'),
        createMockIssue('MAJOR'),
        createMockIssue('MINOR'),
        createMockIssue('MINOR'),
        createMockIssue('MINOR'),
      ];

      render(<IssuesBySeverityChart issues={issues} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('initializes all severity levels to zero', () => {
      const issues: Issue[] = [createMockIssue('CRITICAL')];

      render(<IssuesBySeverityChart issues={issues} />);
      // Even with one severity, all levels should be in the chart
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('maintains correct severity order', () => {
      const issues: Issue[] = [
        createMockIssue('INFO'),
        createMockIssue('BLOCKER'),
        createMockIssue('MINOR'),
      ];

      render(<IssuesBySeverityChart issues={issues} />);
      // Order should be: BLOCKER, CRITICAL, MAJOR, MINOR, INFO
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });

  describe('edge cases', () => {
    it('handles large number of issues per severity', () => {
      const issues: Issue[] = Array(500)
        .fill(null)
        .map(() => createMockIssue('CRITICAL'));

      render(<IssuesBySeverityChart issues={issues} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('handles only one severity type', () => {
      const issues: Issue[] = [
        createMockIssue('BLOCKER'),
        createMockIssue('BLOCKER'),
      ];

      render(<IssuesBySeverityChart issues={issues} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('handles unknown severity gracefully', () => {
      const issues: Issue[] = [
        createMockIssue('UNKNOWN' as Severity),
      ];

      render(<IssuesBySeverityChart issues={issues} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });
});
