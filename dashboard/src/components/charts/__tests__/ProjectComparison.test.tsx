/**
 * Tests for ProjectComparison component
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import ProjectComparisonChart, { TeamComparison } from '../ProjectComparison';
import { ProjectSummary, TeamSummary, QualityGrade, QualityGateStatus } from '../../../types';

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

const createMockProjectSummary = (overrides: Partial<ProjectSummary> = {}): ProjectSummary => ({
  project_name: 'test-project',
  quality_gate_status: 'PASSED' as QualityGateStatus,
  vulnerabilities: 5,
  bugs: 10,
  code_smells: 50,
  coverage: 75.5,
  duplications: 5.2,
  lines_of_code: 10000,
  security_rating: 'B' as QualityGrade,
  reliability_rating: 'A' as QualityGrade,
  maintainability_rating: 'B' as QualityGrade,
  ...overrides,
});

const createMockTeamSummary = (overrides: Partial<TeamSummary> = {}): TeamSummary => ({
  team_id: 'team-1',
  team_name: 'Alpha Team',
  projects: ['project-1', 'project-2'],
  aggregate_metrics: {
    total_issues: 150,
    total_vulnerabilities: 10,
    average_coverage: 72.5,
    average_quality_rating: 'B' as QualityGrade,
  },
  ...overrides,
});

describe('ProjectComparisonChart', () => {
  describe('rendering', () => {
    it('renders without crashing', () => {
      const projects = [
        createMockProjectSummary({ project_name: 'project-1' }),
        createMockProjectSummary({ project_name: 'project-2' }),
      ];

      render(<ProjectComparisonChart projects={projects} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('displays empty state when no projects', () => {
      render(<ProjectComparisonChart projects={[]} />);
      expect(screen.getByText('Select projects to compare')).toBeInTheDocument();
    });

    it('renders view toggle buttons', () => {
      const projects = [createMockProjectSummary()];
      render(<ProjectComparisonChart projects={projects} />);

      expect(screen.getByText('Bar Chart')).toBeInTheDocument();
      expect(screen.getByText('Radar Chart')).toBeInTheDocument();
      expect(screen.getByText('Table')).toBeInTheDocument();
    });

    it('shows best performer indicator', () => {
      const projects = [
        createMockProjectSummary({ project_name: 'best-project', vulnerabilities: 0 }),
        createMockProjectSummary({ project_name: 'other-project', vulnerabilities: 10 }),
      ];

      render(<ProjectComparisonChart projects={projects} />);
      expect(screen.getByText('Best:')).toBeInTheDocument();
    });
  });

  describe('view switching', () => {
    it('switches to radar chart view', () => {
      const projects = [
        createMockProjectSummary({ project_name: 'project-1' }),
        createMockProjectSummary({ project_name: 'project-2' }),
      ];

      render(<ProjectComparisonChart projects={projects} />);
      fireEvent.click(screen.getByText('Radar Chart'));

      expect(screen.getByText('Radar Chart').closest('button')).toHaveClass('bg-primary-100');
    });

    it('switches to table view', () => {
      const projects = [
        createMockProjectSummary({ project_name: 'project-1' }),
        createMockProjectSummary({ project_name: 'project-2' }),
      ];

      render(<ProjectComparisonChart projects={projects} />);
      fireEvent.click(screen.getByText('Table'));

      // Table headers should be visible
      expect(screen.getByText('Project')).toBeInTheDocument();
      expect(screen.getByText('Quality Gate')).toBeInTheDocument();
    });

    it('switches back to bar chart view', () => {
      const projects = [createMockProjectSummary()];
      render(<ProjectComparisonChart projects={projects} />);

      fireEvent.click(screen.getByText('Table'));
      fireEvent.click(screen.getByText('Bar Chart'));

      expect(screen.getByText('Bar Chart').closest('button')).toHaveClass('bg-primary-100');
    });
  });

  describe('bar chart view', () => {
    it('renders bar chart with project data', () => {
      const projects = [
        createMockProjectSummary({ project_name: 'project-1', vulnerabilities: 5 }),
        createMockProjectSummary({ project_name: 'project-2', vulnerabilities: 10 }),
      ];

      render(<ProjectComparisonChart projects={projects} view="bar" />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('renders with custom metrics selection', () => {
      const projects = [createMockProjectSummary()];
      render(
        <ProjectComparisonChart
          projects={projects}
          view="bar"
          metrics={['vulnerabilities', 'bugs']}
        />
      );
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });

  describe('table view', () => {
    it('displays project names in table', () => {
      const projects = [
        createMockProjectSummary({ project_name: 'my-web-app' }),
        createMockProjectSummary({ project_name: 'api-service' }),
      ];

      render(<ProjectComparisonChart projects={projects} view="table" />);
      fireEvent.click(screen.getByText('Table'));

      expect(screen.getByText('my-web-app')).toBeInTheDocument();
      expect(screen.getByText('api-service')).toBeInTheDocument();
    });

    it('displays quality gate status', () => {
      const projects = [
        createMockProjectSummary({ quality_gate_status: 'PASSED' }),
        createMockProjectSummary({ project_name: 'failing', quality_gate_status: 'FAILED' }),
      ];

      render(<ProjectComparisonChart projects={projects} view="table" />);
      fireEvent.click(screen.getByText('Table'));

      expect(screen.getByText('PASSED')).toBeInTheDocument();
      expect(screen.getByText('FAILED')).toBeInTheDocument();
    });

    it('displays vulnerability counts', () => {
      const projects = [
        createMockProjectSummary({ vulnerabilities: 15 }),
      ];

      render(<ProjectComparisonChart projects={projects} view="table" />);
      fireEvent.click(screen.getByText('Table'));

      expect(screen.getByText('15')).toBeInTheDocument();
    });

    it('displays coverage percentage', () => {
      const projects = [
        createMockProjectSummary({ coverage: 82.5 }),
      ];

      render(<ProjectComparisonChart projects={projects} view="table" />);
      fireEvent.click(screen.getByText('Table'));

      expect(screen.getByText('82.5%')).toBeInTheDocument();
    });

    it('displays quality ratings', () => {
      const projects = [
        createMockProjectSummary({
          security_rating: 'A',
          reliability_rating: 'B',
          maintainability_rating: 'C',
        }),
      ];

      render(<ProjectComparisonChart projects={projects} view="table" />);
      fireEvent.click(screen.getByText('Table'));

      expect(screen.getByText('A')).toBeInTheDocument();
      expect(screen.getByText('B')).toBeInTheDocument();
      expect(screen.getByText('C')).toBeInTheDocument();
    });

    it('shows trophy icon for best project', () => {
      const projects = [
        createMockProjectSummary({ project_name: 'best-project', vulnerabilities: 0, bugs: 0 }),
        createMockProjectSummary({ project_name: 'other-project', vulnerabilities: 10, bugs: 20 }),
      ];

      render(<ProjectComparisonChart projects={projects} view="table" />);
      fireEvent.click(screen.getByText('Table'));

      // Trophy icon should be present for best project
      expect(screen.getByText('best-project')).toBeInTheDocument();
    });

    it('displays missing coverage as dash', () => {
      const projects = [
        createMockProjectSummary({ coverage: undefined }),
      ];

      render(<ProjectComparisonChart projects={projects} view="table" />);
      fireEvent.click(screen.getByText('Table'));

      expect(screen.getByText('-')).toBeInTheDocument();
    });
  });

  describe('radar chart view', () => {
    it('renders radar chart with normalized data', () => {
      const projects = [
        createMockProjectSummary({ project_name: 'project-1' }),
        createMockProjectSummary({ project_name: 'project-2' }),
      ];

      render(<ProjectComparisonChart projects={projects} view="radar" />);
      fireEvent.click(screen.getByText('Radar Chart'));

      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });

  describe('comparison data', () => {
    it('uses comparison data for best/worst calculations', () => {
      const projects = [
        createMockProjectSummary({ project_name: 'project-1' }),
        createMockProjectSummary({ project_name: 'project-2' }),
      ];

      const comparisonData = {
        metrics_comparison: [
          { metric: 'vulnerabilities', best_project: 'project-1', worst_project: 'project-2' },
          { metric: 'bugs', best_project: 'project-1', worst_project: 'project-2' },
        ],
      };

      render(<ProjectComparisonChart projects={projects} comparisonData={comparisonData} />);
      expect(screen.getByText('project-1')).toBeInTheDocument();
    });
  });

  describe('edge cases', () => {
    it('handles single project', () => {
      const projects = [createMockProjectSummary()];
      render(<ProjectComparisonChart projects={projects} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('handles many projects', () => {
      const projects = Array(10)
        .fill(null)
        .map((_, i) => createMockProjectSummary({ project_name: `project-${i}` }));

      render(<ProjectComparisonChart projects={projects} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('handles projects with zero values', () => {
      const projects = [
        createMockProjectSummary({
          vulnerabilities: 0,
          bugs: 0,
          code_smells: 0,
          coverage: 0,
        }),
      ];

      render(<ProjectComparisonChart projects={projects} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('handles projects with all same values', () => {
      const projects = [
        createMockProjectSummary({ project_name: 'project-1' }),
        createMockProjectSummary({ project_name: 'project-2' }),
      ];

      render(<ProjectComparisonChart projects={projects} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });
});

describe('TeamComparison', () => {
  describe('rendering', () => {
    it('renders without crashing', () => {
      const teams = [createMockTeamSummary()];
      render(<TeamComparison teams={teams} />);
      expect(screen.getByText('Team Comparison')).toBeInTheDocument();
    });

    it('displays empty state when no teams', () => {
      render(<TeamComparison teams={[]} />);
      expect(screen.getByText('No teams to compare')).toBeInTheDocument();
    });

    it('renders team cards', () => {
      const teams = [
        createMockTeamSummary({ team_name: 'Alpha Team' }),
        createMockTeamSummary({ team_name: 'Beta Team', team_id: 'team-2' }),
      ];

      render(<TeamComparison teams={teams} />);
      expect(screen.getByText('Alpha Team')).toBeInTheDocument();
      expect(screen.getByText('Beta Team')).toBeInTheDocument();
    });

    it('displays project count', () => {
      const teams = [
        createMockTeamSummary({ projects: ['p1', 'p2', 'p3'] }),
      ];

      render(<TeamComparison teams={teams} />);
      expect(screen.getByText('3 projects')).toBeInTheDocument();
    });

    it('displays total issues', () => {
      const teams = [
        createMockTeamSummary({
          aggregate_metrics: {
            total_issues: 250,
            total_vulnerabilities: 15,
            average_coverage: 70,
            average_quality_rating: 'B',
          },
        }),
      ];

      render(<TeamComparison teams={teams} />);
      expect(screen.getByText('250')).toBeInTheDocument();
    });

    it('displays total vulnerabilities', () => {
      const teams = [
        createMockTeamSummary({
          aggregate_metrics: {
            total_issues: 100,
            total_vulnerabilities: 25,
            average_coverage: 70,
            average_quality_rating: 'B',
          },
        }),
      ];

      render(<TeamComparison teams={teams} />);
      expect(screen.getByText('25')).toBeInTheDocument();
    });

    it('displays average coverage', () => {
      const teams = [
        createMockTeamSummary({
          aggregate_metrics: {
            total_issues: 100,
            total_vulnerabilities: 10,
            average_coverage: 85.5,
            average_quality_rating: 'A',
          },
        }),
      ];

      render(<TeamComparison teams={teams} />);
      expect(screen.getByText('85.5%')).toBeInTheDocument();
    });

    it('displays average quality rating', () => {
      const teams = [
        createMockTeamSummary({
          aggregate_metrics: {
            total_issues: 100,
            total_vulnerabilities: 10,
            average_coverage: 80,
            average_quality_rating: 'A',
          },
        }),
      ];

      render(<TeamComparison teams={teams} />);
      // Rating badge should show 'A'
      const ratingBadges = screen.getAllByText('A');
      expect(ratingBadges.length).toBeGreaterThan(0);
    });

    it('renders bar chart for team comparison', () => {
      const teams = [
        createMockTeamSummary({ team_name: 'Team 1' }),
        createMockTeamSummary({ team_name: 'Team 2', team_id: 'team-2' }),
      ];

      render(<TeamComparison teams={teams} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });

  describe('edge cases', () => {
    it('handles single team', () => {
      const teams = [createMockTeamSummary()];
      render(<TeamComparison teams={teams} />);
      expect(screen.getByText('Alpha Team')).toBeInTheDocument();
    });

    it('handles team with no projects', () => {
      const teams = [
        createMockTeamSummary({ projects: [] }),
      ];

      render(<TeamComparison teams={teams} />);
      expect(screen.getByText('0 projects')).toBeInTheDocument();
    });

    it('handles many teams', () => {
      const teams = Array(8)
        .fill(null)
        .map((_, i) => createMockTeamSummary({ team_id: `team-${i}`, team_name: `Team ${i}` }));

      render(<TeamComparison teams={teams} />);
      expect(screen.getByText('Team 0')).toBeInTheDocument();
      expect(screen.getByText('Team 7')).toBeInTheDocument();
    });
  });
});
