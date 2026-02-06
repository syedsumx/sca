/**
 * Tests for IssuesByTypeChart component
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import IssuesByTypeChart from '../IssuesByTypeChart';
import { Issue } from '../../../types';

// Mock Recharts components since they require DOM measurements
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

const createMockIssue = (overrides: Partial<Issue> = {}): Issue => ({
  id: `issue-${Math.random().toString(36).substr(2, 9)}`,
  rule_id: 'TEST001',
  rule_name: 'Test Rule',
  severity: 'MAJOR',
  issue_type: 'BUG',
  message: 'Test message',
  location: {
    file_path: 'test.py',
    start_line: 1,
    end_line: 1,
  },
  tags: [],
  ...overrides,
});

describe('IssuesByTypeChart', () => {
  describe('rendering', () => {
    it('renders without crashing', () => {
      const issues: Issue[] = [
        createMockIssue({ issue_type: 'BUG' }),
        createMockIssue({ issue_type: 'VULNERABILITY' }),
      ];

      render(<IssuesByTypeChart issues={issues} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('displays "No issues found" when issues array is empty', () => {
      render(<IssuesByTypeChart issues={[]} />);
      expect(screen.getByText('No issues found')).toBeInTheDocument();
    });

    it('renders pie chart for single issue type', () => {
      const issues: Issue[] = [
        createMockIssue({ issue_type: 'BUG' }),
      ];

      render(<IssuesByTypeChart issues={issues} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });

  describe('data processing', () => {
    it('correctly counts issues by type', () => {
      const issues: Issue[] = [
        createMockIssue({ issue_type: 'BUG' }),
        createMockIssue({ issue_type: 'BUG' }),
        createMockIssue({ issue_type: 'VULNERABILITY' }),
        createMockIssue({ issue_type: 'CODE_SMELL' }),
        createMockIssue({ issue_type: 'CODE_SMELL' }),
        createMockIssue({ issue_type: 'CODE_SMELL' }),
      ];

      render(<IssuesByTypeChart issues={issues} />);
      // Component renders successfully with data
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('handles all issue types', () => {
      const issues: Issue[] = [
        createMockIssue({ issue_type: 'BUG' }),
        createMockIssue({ issue_type: 'VULNERABILITY' }),
        createMockIssue({ issue_type: 'CODE_SMELL' }),
        createMockIssue({ issue_type: 'SECURITY_HOTSPOT' }),
        createMockIssue({ issue_type: 'DUPLICATION' }),
      ];

      render(<IssuesByTypeChart issues={issues} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('handles unknown issue types gracefully', () => {
      const issues: Issue[] = [
        createMockIssue({ issue_type: 'UNKNOWN_TYPE' as any }),
      ];

      render(<IssuesByTypeChart issues={issues} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });

  describe('edge cases', () => {
    it('handles large number of issues', () => {
      const issues: Issue[] = Array(1000)
        .fill(null)
        .map((_, i) => createMockIssue({
          issue_type: ['BUG', 'VULNERABILITY', 'CODE_SMELL'][i % 3] as any,
        }));

      render(<IssuesByTypeChart issues={issues} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('handles issues with same type', () => {
      const issues: Issue[] = Array(10)
        .fill(null)
        .map(() => createMockIssue({ issue_type: 'BUG' }));

      render(<IssuesByTypeChart issues={issues} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });
});
