import React, { useState } from 'react';
import {
  ArrowsRightLeftIcon,
  PlusCircleIcon,
  MinusCircleIcon,
  BuildingOfficeIcon,
  FolderIcon,
} from '@heroicons/react/24/outline';
import { Card, CardHeader, CardBody } from '../../components/common';
import { ProjectComparisonChart, TeamComparison } from '../../components/charts';
import { ProjectSummary, TeamSummary, QualityGrade, QualityGateStatus } from '../../types';

const mockComparison = {
  base_id: 'main-2025-01-28',
  head_id: 'feature/auth-2025-01-30',
  summary: {
    base_total: 470,
    head_total: 518,
    new_issues: 62,
    fixed_issues: 14,
    unchanged: 456,
    net_change: 48,
  },
  new_by_severity: { BLOCKER: 2, MAJOR: 35, MINOR: 20, INFO: 5 },
  new_issues: [
    { rule_id: 'python:S138', severity: 'MAJOR', type: 'CODE_SMELL', message: "Function 'sso_callback' has 67 lines (max: 50)", file: 'src/codescope/api/routes/auth.py', line: 180 },
    { rule_id: 'python:S138', severity: 'MAJOR', type: 'CODE_SMELL', message: "Function 'get_current_user' has 67 lines (max: 50)", file: 'src/codescope/auth/middleware.py', line: 45 },
    { rule_id: 'python:S1192', severity: 'MINOR', type: 'CODE_SMELL', message: 'String literal "expires_at" duplicated 5 times', file: 'src/codescope/auth/database.py', line: 82 },
    { rule_id: 'python:S510', severity: 'BLOCKER', type: 'VULNERABILITY', message: "open() with user-controlled path", file: 'src/codescope/auth/tokens.py', line: 34 },
  ],
  fixed_issues: [
    { rule_id: 'python:S501', severity: 'MAJOR', type: 'CODE_SMELL', message: 'Bare except with pass', file: 'src/codescope/analyzers/coverage/formats.py', line: 55 },
    { rule_id: 'python:S501', severity: 'MAJOR', type: 'CODE_SMELL', message: 'Bare except with pass', file: 'src/codescope/analyzers/coverage/formats.py', line: 82 },
  ],
};

// Mock project data for comparison
const mockProjects: ProjectSummary[] = [
  {
    project_name: 'codescope-api',
    quality_gate_status: 'PASSED' as QualityGateStatus,
    vulnerabilities: 5,
    bugs: 12,
    code_smells: 85,
    coverage: 78.5,
    duplications: 3.2,
    lines_of_code: 15000,
    security_rating: 'B' as QualityGrade,
    reliability_rating: 'A' as QualityGrade,
    maintainability_rating: 'A' as QualityGrade,
  },
  {
    project_name: 'codescope-dashboard',
    quality_gate_status: 'PASSED' as QualityGateStatus,
    vulnerabilities: 2,
    bugs: 8,
    code_smells: 45,
    coverage: 65.2,
    duplications: 5.1,
    lines_of_code: 8500,
    security_rating: 'A' as QualityGrade,
    reliability_rating: 'A' as QualityGrade,
    maintainability_rating: 'B' as QualityGrade,
  },
  {
    project_name: 'codescope-cli',
    quality_gate_status: 'WARNING' as QualityGateStatus,
    vulnerabilities: 8,
    bugs: 15,
    code_smells: 120,
    coverage: 55.8,
    duplications: 8.5,
    lines_of_code: 12000,
    security_rating: 'C' as QualityGrade,
    reliability_rating: 'B' as QualityGrade,
    maintainability_rating: 'C' as QualityGrade,
  },
  {
    project_name: 'codescope-rules',
    quality_gate_status: 'PASSED' as QualityGateStatus,
    vulnerabilities: 0,
    bugs: 5,
    code_smells: 32,
    coverage: 92.3,
    duplications: 1.2,
    lines_of_code: 6000,
    security_rating: 'A' as QualityGrade,
    reliability_rating: 'A' as QualityGrade,
    maintainability_rating: 'A' as QualityGrade,
  },
  {
    project_name: 'codescope-parsers',
    quality_gate_status: 'FAILED' as QualityGateStatus,
    vulnerabilities: 12,
    bugs: 22,
    code_smells: 180,
    coverage: 42.5,
    duplications: 12.8,
    lines_of_code: 18000,
    security_rating: 'D' as QualityGrade,
    reliability_rating: 'C' as QualityGrade,
    maintainability_rating: 'D' as QualityGrade,
  },
];

// Mock team data for comparison
const mockTeams: TeamSummary[] = [
  {
    team_id: 'team-platform',
    team_name: 'Platform Team',
    projects: ['codescope-api', 'codescope-cli'],
    aggregate_metrics: {
      total_issues: 245,
      total_vulnerabilities: 13,
      average_coverage: 67.2,
      average_duplication: 5.9,
      average_quality_rating: 'B' as QualityGrade,
    },
  },
  {
    team_id: 'team-frontend',
    team_name: 'Frontend Team',
    projects: ['codescope-dashboard'],
    aggregate_metrics: {
      total_issues: 55,
      total_vulnerabilities: 2,
      average_coverage: 65.2,
      average_duplication: 5.1,
      average_quality_rating: 'A' as QualityGrade,
    },
  },
  {
    team_id: 'team-analysis',
    team_name: 'Analysis Team',
    projects: ['codescope-rules', 'codescope-parsers'],
    aggregate_metrics: {
      total_issues: 251,
      total_vulnerabilities: 12,
      average_coverage: 67.4,
      average_duplication: 7.0,
      average_quality_rating: 'B' as QualityGrade,
    },
  },
  {
    team_id: 'team-security',
    team_name: 'Security Team',
    projects: ['codescope-api', 'codescope-rules'],
    aggregate_metrics: {
      total_issues: 139,
      total_vulnerabilities: 5,
      average_coverage: 85.4,
      average_duplication: 2.2,
      average_quality_rating: 'A' as QualityGrade,
    },
  },
];

const sevColor: Record<string, string> = {
  BLOCKER: 'bg-red-100 text-red-800',
  MAJOR: 'bg-orange-100 text-orange-800',
  MINOR: 'bg-yellow-100 text-yellow-800',
  INFO: 'bg-blue-100 text-blue-800',
};

type CompareTab = 'diff' | 'projects' | 'teams';

const Compare: React.FC = () => {
  const [activeTab, setActiveTab] = useState<CompareTab>('diff');
  const { summary } = mockComparison;

  const tabs = [
    { id: 'diff' as CompareTab, label: 'PR Diff', icon: ArrowsRightLeftIcon },
    { id: 'projects' as CompareTab, label: 'Project Comparison', icon: FolderIcon },
    { id: 'teams' as CompareTab, label: 'Team Comparison', icon: BuildingOfficeIcon },
  ];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Compare & Analyze</h1>
        <p className="mt-1 text-sm text-gray-500">
          Compare code quality across branches, projects, and teams
        </p>
      </div>

      {/* Tab Navigation */}
      <div className="border-b border-gray-200">
        <nav className="flex space-x-4" aria-label="Tabs">
          {tabs.map((tab) => {
            const Icon = tab.icon;
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-2 px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  activeTab === tab.id
                    ? 'border-primary-500 text-primary-600'
                    : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                }`}
              >
                <Icon className="w-4 h-4" />
                {tab.label}
              </button>
            );
          })}
        </nav>
      </div>

      {/* PR Diff Tab */}
      {activeTab === 'diff' && (
        <div className="space-y-6">
          {/* Branch Comparison Header */}
          <div className="bg-white rounded-lg border p-4 flex items-center justify-center gap-4">
            <div className="text-center">
              <p className="text-sm text-gray-500">Base</p>
              <p className="font-mono font-medium text-gray-900">{mockComparison.base_id}</p>
              <p className="text-sm text-gray-400">{summary.base_total} issues</p>
            </div>
            <ArrowsRightLeftIcon className="w-5 h-5 text-gray-400" />
            <div className="text-center">
              <p className="text-sm text-gray-500">Head</p>
              <p className="font-mono font-medium text-gray-900">{mockComparison.head_id}</p>
              <p className="text-sm text-gray-400">{summary.head_total} issues</p>
            </div>
          </div>

          {/* Summary Cards */}
          <div className="grid grid-cols-4 gap-4">
            <div className="bg-red-50 rounded-lg border border-red-200 p-4 text-center">
              <PlusCircleIcon className="w-6 h-6 text-red-500 mx-auto" />
              <p className="text-2xl font-bold text-red-700 mt-1">{summary.new_issues}</p>
              <p className="text-sm text-red-600">New Issues</p>
            </div>
            <div className="bg-green-50 rounded-lg border border-green-200 p-4 text-center">
              <MinusCircleIcon className="w-6 h-6 text-green-500 mx-auto" />
              <p className="text-2xl font-bold text-green-700 mt-1">{summary.fixed_issues}</p>
              <p className="text-sm text-green-600">Fixed Issues</p>
            </div>
            <div className="bg-gray-50 rounded-lg border p-4 text-center">
              <p className="text-2xl font-bold text-gray-700">{summary.unchanged}</p>
              <p className="text-sm text-gray-500">Unchanged</p>
            </div>
            <div className={`rounded-lg border p-4 text-center ${summary.net_change > 0 ? 'bg-red-50 border-red-200' : 'bg-green-50 border-green-200'}`}>
              <p className={`text-2xl font-bold ${summary.net_change > 0 ? 'text-red-700' : 'text-green-700'}`}>
                {summary.net_change > 0 ? '+' : ''}{summary.net_change}
              </p>
              <p className="text-sm text-gray-500">Net Change</p>
            </div>
          </div>

          {/* New Issues */}
          <Card>
            <CardHeader
              title={`New Issues (${mockComparison.new_issues.length} shown)`}
              subtitle="Issues introduced in the head branch"
            />
            <CardBody className="p-0">
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50">
                    <tr>
                      {['Severity', 'Type', 'Rule', 'Message', 'File', 'Line'].map(h => (
                        <th key={h} className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200">
                    {mockComparison.new_issues.map((issue, i) => (
                      <tr key={i} className="hover:bg-gray-50">
                        <td className="px-4 py-2"><span className={`px-2 py-0.5 rounded-full text-xs font-medium ${sevColor[issue.severity]}`}>{issue.severity}</span></td>
                        <td className="px-4 py-2 text-xs">{issue.type}</td>
                        <td className="px-4 py-2 text-xs font-mono">{issue.rule_id}</td>
                        <td className="px-4 py-2 text-sm">{issue.message}</td>
                        <td className="px-4 py-2 text-xs font-mono text-gray-500">{issue.file}</td>
                        <td className="px-4 py-2 text-sm">{issue.line}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CardBody>
          </Card>

          {/* Fixed Issues */}
          <Card>
            <CardHeader
              title={`Fixed Issues (${mockComparison.fixed_issues.length} shown)`}
              subtitle="Issues resolved in the head branch"
            />
            <CardBody className="p-0">
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50">
                    <tr>
                      {['Severity', 'Rule', 'Message', 'File'].map(h => (
                        <th key={h} className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200">
                    {mockComparison.fixed_issues.map((issue, i) => (
                      <tr key={i} className="hover:bg-gray-50">
                        <td className="px-4 py-2"><span className={`px-2 py-0.5 rounded-full text-xs font-medium ${sevColor[issue.severity]}`}>{issue.severity}</span></td>
                        <td className="px-4 py-2 text-xs font-mono">{issue.rule_id}</td>
                        <td className="px-4 py-2 text-sm">{issue.message}</td>
                        <td className="px-4 py-2 text-xs font-mono text-gray-500">{issue.file}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CardBody>
          </Card>
        </div>
      )}

      {/* Project Comparison Tab */}
      {activeTab === 'projects' && (
        <Card>
          <CardHeader
            title="Project Comparison"
            subtitle="Compare code quality metrics across all projects"
          />
          <CardBody>
            <ProjectComparisonChart
              projects={mockProjects}
              view="bar"
              metrics={['vulnerabilities', 'bugs', 'code_smells']}
            />
          </CardBody>
        </Card>
      )}

      {/* Team Comparison Tab */}
      {activeTab === 'teams' && (
        <Card>
          <CardHeader
            title="Team Comparison"
            subtitle="Compare aggregate metrics across teams"
          />
          <CardBody>
            <TeamComparison teams={mockTeams} />
          </CardBody>
        </Card>
      )}
    </div>
  );
};

export default Compare;
