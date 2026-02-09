import React, { useState, useEffect } from 'react';
import {
  ShieldExclamationIcon,
  BugAntIcon,
  BeakerIcon,
  DocumentDuplicateIcon,
  CheckCircleIcon,
  ArrowDownTrayIcon,
  ChartBarIcon,
  FireIcon,
  ClockIcon,
} from '@heroicons/react/24/outline';
import { Card, CardHeader, CardBody, MetricCard, LoadingPage, StatusBadge } from '../../components/common';
import {
  IssuesByTypeChart,
  IssuesBySeverityChart,
  TrendLineChart,
  VulnerabilityTimeline,
  HotspotHeatmap,
} from '../../components/charts';
import { formatRelativeTime, formatNumber } from '../../utils/format';
import {
  DashboardStats,
  Issue,
  QualityGateStatus,
  TrendSnapshot,
  TimelineEvent,
  HotspotData,
  Severity,
} from '../../types';
import api from '../../services/api';

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

// Mock trend data for visualization
const mockTrendData: TrendSnapshot[] = [
  { timestamp: '2025-01-20T10:00:00Z', total_issues: 285, vulnerabilities: 18, bugs: 42, code_smells: 225, coverage: 68.5, quality_gate_status: 'FAILED' },
  { timestamp: '2025-01-21T10:00:00Z', total_issues: 278, vulnerabilities: 16, bugs: 40, code_smells: 222, coverage: 70.2, quality_gate_status: 'FAILED' },
  { timestamp: '2025-01-22T10:00:00Z', total_issues: 265, vulnerabilities: 15, bugs: 38, code_smells: 212, coverage: 72.0, quality_gate_status: 'FAILED' },
  { timestamp: '2025-01-23T10:00:00Z', total_issues: 258, vulnerabilities: 14, bugs: 35, code_smells: 209, coverage: 74.5, quality_gate_status: 'WARNING' },
  { timestamp: '2025-01-24T10:00:00Z', total_issues: 250, vulnerabilities: 13, bugs: 32, code_smells: 205, coverage: 76.8, quality_gate_status: 'WARNING' },
  { timestamp: '2025-01-25T10:00:00Z', total_issues: 247, vulnerabilities: 12, bugs: 30, code_smells: 205, coverage: 78.2, quality_gate_status: 'PASSED' },
];

// Mock timeline events
const mockTimelineEvents: TimelineEvent[] = [
  {
    id: 'evt-1',
    timestamp: new Date(Date.now() - 1800000).toISOString(),
    event_type: 'vulnerability_found',
    severity: 'CRITICAL' as Severity,
    title: 'SQL Injection Detected',
    description: 'Critical SQL injection vulnerability found in user authentication module',
    file_path: 'src/auth/login.py',
    rule_id: 'S001',
    project_name: 'my-web-app',
  },
  {
    id: 'evt-2',
    timestamp: new Date(Date.now() - 3600000).toISOString(),
    event_type: 'issue_fixed',
    severity: 'MAJOR' as Severity,
    title: 'Buffer Overflow Fixed',
    description: 'Fixed buffer overflow vulnerability in data processing module',
    file_path: 'src/core/processor.py',
    rule_id: 'S012',
    project_name: 'api-service',
  },
  {
    id: 'evt-3',
    timestamp: new Date(Date.now() - 7200000).toISOString(),
    event_type: 'scan_complete',
    title: 'Analysis Complete',
    description: 'Full project scan completed with 247 issues found',
    project_name: 'my-web-app',
  },
  {
    id: 'evt-4',
    timestamp: new Date(Date.now() - 14400000).toISOString(),
    event_type: 'quality_gate_change',
    title: 'Quality Gate Passed',
    description: 'Project now meets all quality gate conditions',
    project_name: 'mobile-app',
  },
  {
    id: 'evt-5',
    timestamp: new Date(Date.now() - 28800000).toISOString(),
    event_type: 'vulnerability_found',
    severity: 'BLOCKER' as Severity,
    title: 'Command Injection Found',
    description: 'Blocker-level command injection in shell executor',
    file_path: 'src/utils/shell.py',
    rule_id: 'S002',
    project_name: 'api-service',
  },
];

// Mock hotspot data
const mockHotspotData: HotspotData = {
  files: [
    { file_path: 'src/api/handlers/user.py', risk_score: 92, issues_count: 18, complexity: 45, churn: 25, authors: ['alice', 'bob', 'charlie'], last_modified: '2025-01-25T10:00:00Z' },
    { file_path: 'src/core/auth/session.py', risk_score: 85, issues_count: 14, complexity: 38, churn: 20, authors: ['alice', 'david'], last_modified: '2025-01-24T10:00:00Z' },
    { file_path: 'src/services/payment.py', risk_score: 78, issues_count: 12, complexity: 32, churn: 18, authors: ['bob', 'eve'], last_modified: '2025-01-23T10:00:00Z' },
    { file_path: 'src/utils/validators.py', risk_score: 65, issues_count: 8, complexity: 25, churn: 15, authors: ['charlie'], last_modified: '2025-01-22T10:00:00Z' },
    { file_path: 'src/models/order.py', risk_score: 52, issues_count: 6, complexity: 20, churn: 12, authors: ['david', 'frank'], last_modified: '2025-01-21T10:00:00Z' },
  ],
  directories: [
    { path: 'src/api/handlers', risk_score: 88, total_issues: 45, total_files: 12, average_complexity: 35.5 },
    { path: 'src/core/auth', risk_score: 75, total_issues: 28, total_files: 8, average_complexity: 28.2 },
    { path: 'src/services', risk_score: 62, total_issues: 22, total_files: 10, average_complexity: 22.8 },
    { path: 'src/utils', risk_score: 48, total_issues: 15, total_files: 6, average_complexity: 18.5 },
  ],
};

type DashboardTab = 'overview' | 'trends' | 'timeline' | 'hotspots';

const Dashboard: React.FC = () => {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [activeTab, setActiveTab] = useState<DashboardTab>('overview');

  useEffect(() => {
    // Simulate API call
    setTimeout(() => {
      setStats(mockStats);
      setLoading(false);
    }, 500);
  }, []);

  const handleExportPdf = async (analysisId: string) => {
    setExporting(true);
    try {
      const blob = await api.exportPdf(analysisId);
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `codescope-report-${analysisId}.pdf`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch {
      alert('Failed to export PDF. Make sure the analysis is complete and the server is running.');
    } finally {
      setExporting(false);
    }
  };

  if (loading || !stats) {
    return <LoadingPage message="Loading dashboard..." />;
  }

  const tabs = [
    { id: 'overview' as DashboardTab, label: 'Overview', icon: ChartBarIcon },
    { id: 'trends' as DashboardTab, label: 'Trends', icon: BeakerIcon },
    { id: 'timeline' as DashboardTab, label: 'Activity', icon: ClockIcon },
    { id: 'hotspots' as DashboardTab, label: 'Hotspots', icon: FireIcon },
  ];

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Dashboard</h1>
          <p className="mt-1 text-sm text-gray-500">
            Overview of your code quality and security analysis
          </p>
        </div>
        {stats.recent_analyses.length > 0 && (
          <button
            onClick={() => handleExportPdf(stats.recent_analyses[0].analysis_id)}
            disabled={exporting}
            className="inline-flex items-center gap-2 px-4 py-2 bg-primary-600 text-white text-sm font-medium rounded-lg hover:bg-primary-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <ArrowDownTrayIcon className="w-4 h-4" />
            {exporting ? 'Exporting...' : 'Export PDF'}
          </button>
        )}
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

      {/* Tab Content */}
      {activeTab === 'overview' && (
        <div className="space-y-6">
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
                      <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">
                        Actions
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
                        <td className="px-6 py-4 whitespace-nowrap text-right">
                          <button
                            onClick={() => handleExportPdf(analysis.analysis_id)}
                            disabled={exporting}
                            className="inline-flex items-center gap-1 px-3 py-1.5 text-xs font-medium text-primary-600 bg-primary-50 rounded-md hover:bg-primary-100 disabled:opacity-50 transition-colors"
                            title="Export as PDF"
                          >
                            <ArrowDownTrayIcon className="w-3.5 h-3.5" />
                            PDF
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CardBody>
          </Card>
        </div>
      )}

      {activeTab === 'trends' && (
        <div className="space-y-6">
          <Card>
            <CardHeader
              title="Issue Trends"
              subtitle="Track your code quality improvements over time"
            />
            <CardBody>
              <TrendLineChart
                data={mockTrendData}
                metrics={['total_issues', 'vulnerabilities', 'bugs', 'code_smells']}
                height={400}
                showQualityGate={true}
              />
            </CardBody>
          </Card>

          <Card>
            <CardHeader
              title="Coverage Trend"
              subtitle="Code coverage percentage over time"
            />
            <CardBody>
              <TrendLineChart
                data={mockTrendData}
                metrics={['coverage']}
                height={250}
                showQualityGate={false}
              />
            </CardBody>
          </Card>
        </div>
      )}

      {activeTab === 'timeline' && (
        <Card>
          <CardHeader
            title="Recent Activity"
            subtitle="Timeline of security events and analysis results"
          />
          <CardBody>
            <VulnerabilityTimeline
              events={mockTimelineEvents}
              maxEvents={20}
              showFilters={true}
            />
          </CardBody>
        </Card>
      )}

      {activeTab === 'hotspots' && (
        <Card>
          <CardHeader
            title="Code Hotspots"
            subtitle="Files and directories with highest risk scores"
          />
          <CardBody>
            <HotspotHeatmap
              data={mockHotspotData}
              view="files"
              maxItems={10}
              onFileClick={(filePath) => {
                console.log('Navigate to file:', filePath);
              }}
            />
          </CardBody>
        </Card>
      )}
    </div>
  );
};

export default Dashboard;
