import React, { useState, useEffect } from 'react';
import {
  ShieldExclamationIcon,
  BugAntIcon,
  BeakerIcon,
  DocumentDuplicateIcon,
  CheckCircleIcon,
  XCircleIcon,
} from '@heroicons/react/24/outline';
import { Card, CardHeader, CardBody, MetricCard, LoadingPage, StatusBadge } from '../../components/common';
import { IssuesByTypeChart, IssuesBySeverityChart } from '../../components/charts';
import { formatRelativeTime, formatNumber } from '../../utils/format';
import { DashboardStats, Issue, QualityGateStatus } from '../../types';

// Mock data for demonstration
const mockStats: DashboardStats = {
  total_projects: 5,
  total_issues: 247,
  total_vulnerabilities: 12,
  projects_passing: 3,
  projects_failing: 2,
  recent_analyses: [
    {
      project_name: 'my-web-app',
      analysis_id: 'a1b2c3',
      timestamp: new Date(Date.now() - 3600000).toISOString(),
      issues_count: 45,
      quality_gate_status: 'PASSED' as QualityGateStatus,
    },
    {
      project_name: 'api-service',
      analysis_id: 'd4e5f6',
      timestamp: new Date(Date.now() - 7200000).toISOString(),
      issues_count: 78,
      quality_gate_status: 'FAILED' as QualityGateStatus,
    },
    {
      project_name: 'mobile-app',
      analysis_id: 'g7h8i9',
      timestamp: new Date(Date.now() - 86400000).toISOString(),
      issues_count: 23,
      quality_gate_status: 'PASSED' as QualityGateStatus,
    },
  ],
};

const mockIssues: Issue[] = [
  { id: '1', rule_id: 'S001', rule_name: 'SQL Injection', severity: 'CRITICAL', issue_type: 'VULNERABILITY', message: 'SQL injection vulnerability', location: { file_path: 'app.py', start_line: 10, end_line: 10 }, tags: [] },
  { id: '2', rule_id: 'S002', rule_name: 'Command Injection', severity: 'BLOCKER', issue_type: 'VULNERABILITY', message: 'Command injection', location: { file_path: 'utils.py', start_line: 20, end_line: 20 }, tags: [] },
  { id: '3', rule_id: 'B001', rule_name: 'Null Pointer', severity: 'MAJOR', issue_type: 'BUG', message: 'Potential null pointer', location: { file_path: 'service.py', start_line: 30, end_line: 30 }, tags: [] },
  { id: '4', rule_id: 'C001', rule_name: 'Long Function', severity: 'MINOR', issue_type: 'CODE_SMELL', message: 'Function too long', location: { file_path: 'handler.py', start_line: 40, end_line: 40 }, tags: [] },
  { id: '5', rule_id: 'C002', rule_name: 'Complexity', severity: 'MAJOR', issue_type: 'CODE_SMELL', message: 'High complexity', location: { file_path: 'core.py', start_line: 50, end_line: 50 }, tags: [] },
  { id: '6', rule_id: 'S003', rule_name: 'Hardcoded Secret', severity: 'CRITICAL', issue_type: 'VULNERABILITY', message: 'Hardcoded password', location: { file_path: 'config.py', start_line: 60, end_line: 60 }, tags: [] },
  { id: '7', rule_id: 'B002', rule_name: 'Division by Zero', severity: 'MAJOR', issue_type: 'BUG', message: 'Potential division by zero', location: { file_path: 'math.py', start_line: 70, end_line: 70 }, tags: [] },
  { id: '8', rule_id: 'C003', rule_name: 'Dead Code', severity: 'INFO', issue_type: 'CODE_SMELL', message: 'Unreachable code', location: { file_path: 'legacy.py', start_line: 80, end_line: 80 }, tags: [] },
];

const Dashboard: React.FC = () => {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Simulate API call
    setTimeout(() => {
      setStats(mockStats);
      setLoading(false);
    }, 500);
  }, []);

  if (loading || !stats) {
    return <LoadingPage message="Loading dashboard..." />;
  }

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Dashboard</h1>
        <p className="mt-1 text-sm text-gray-500">
          Overview of your code quality and security analysis
        </p>
      </div>

      {/* Summary Metrics */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <MetricCard
          title="Total Issues"
          value={formatNumber(stats.total_issues)}
          subtitle="Across all projects"
          icon={<ShieldExclamationIcon className="w-6 h-6" />}
        />
        <MetricCard
          title="Vulnerabilities"
          value={formatNumber(stats.total_vulnerabilities)}
          subtitle="Security issues"
          icon={<BugAntIcon className="w-6 h-6" />}
        />
        <MetricCard
          title="Projects Passing"
          value={stats.projects_passing}
          subtitle={`${stats.projects_failing} failing`}
          icon={<CheckCircleIcon className="w-6 h-6" />}
        />
        <MetricCard
          title="Total Projects"
          value={stats.total_projects}
          subtitle="Being analyzed"
          icon={<DocumentDuplicateIcon className="w-6 h-6" />}
        />
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card>
          <CardHeader title="Issues by Type" />
          <CardBody>
            <IssuesByTypeChart issues={mockIssues} />
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Issues by Severity" />
          <CardBody>
            <IssuesBySeverityChart issues={mockIssues} />
          </CardBody>
        </Card>
      </div>

      {/* Recent Analyses */}
      <Card>
        <CardHeader
          title="Recent Analyses"
          subtitle="Latest code analysis results"
        />
        <CardBody className="p-0">
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Project
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Status
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Issues
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Analyzed
                  </th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {stats.recent_analyses.map((analysis) => (
                  <tr key={analysis.analysis_id} className="hover:bg-gray-50">
                    <td className="px-6 py-4 whitespace-nowrap">
                      <div className="flex items-center">
                        <div className="flex-shrink-0 h-10 w-10 bg-primary-100 rounded-lg flex items-center justify-center">
                          <BeakerIcon className="h-5 w-5 text-primary-600" />
                        </div>
                        <div className="ml-4">
                          <div className="text-sm font-medium text-gray-900">
                            {analysis.project_name}
                          </div>
                          <div className="text-sm text-gray-500">
                            {analysis.analysis_id}
                          </div>
                        </div>
                      </div>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                      <StatusBadge status={analysis.quality_gate_status} />
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900">
                      {analysis.issues_count}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                      {formatRelativeTime(analysis.timestamp)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardBody>
      </Card>
    </div>
  );
};

export default Dashboard;
