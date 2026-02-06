import React, { useState } from 'react';
import {
  ArrowTrendingUpIcon,
  ArrowTrendingDownIcon,
  ChartBarIcon,
  ClockIcon,
  FireIcon,
} from '@heroicons/react/24/outline';
import { Card, CardHeader, CardBody } from '../../components/common';
import {
  TrendLineChart,
  VulnerabilityTimeline,
  HotspotHeatmap,
} from '../../components/charts';
import {
  TrendSnapshot,
  TimelineEvent,
  HotspotData,
  Severity,
  QualityGateStatus,
} from '../../types';

// Extended mock data for trends
const mockSnapshots: TrendSnapshot[] = [
  { timestamp: '2025-01-15', total_issues: 580, bugs: 125, vulnerabilities: 28, code_smells: 427, coverage: 62.5, quality_gate_status: 'FAILED' as QualityGateStatus },
  { timestamp: '2025-01-16', total_issues: 565, bugs: 120, vulnerabilities: 26, code_smells: 419, coverage: 64.0, quality_gate_status: 'FAILED' as QualityGateStatus },
  { timestamp: '2025-01-17', total_issues: 548, bugs: 118, vulnerabilities: 24, code_smells: 406, coverage: 65.5, quality_gate_status: 'FAILED' as QualityGateStatus },
  { timestamp: '2025-01-18', total_issues: 532, bugs: 115, vulnerabilities: 22, code_smells: 395, coverage: 67.2, quality_gate_status: 'FAILED' as QualityGateStatus },
  { timestamp: '2025-01-19', total_issues: 520, bugs: 110, vulnerabilities: 20, code_smells: 390, coverage: 69.0, quality_gate_status: 'FAILED' as QualityGateStatus },
  { timestamp: '2025-01-20', total_issues: 510, bugs: 108, vulnerabilities: 18, code_smells: 384, coverage: 70.5, quality_gate_status: 'FAILED' as QualityGateStatus },
  { timestamp: '2025-01-21', total_issues: 498, bugs: 105, vulnerabilities: 17, code_smells: 376, coverage: 72.0, quality_gate_status: 'FAILED' as QualityGateStatus },
  { timestamp: '2025-01-22', total_issues: 485, bugs: 102, vulnerabilities: 16, code_smells: 367, coverage: 73.5, quality_gate_status: 'WARNING' as QualityGateStatus },
  { timestamp: '2025-01-23', total_issues: 475, bugs: 100, vulnerabilities: 15, code_smells: 360, coverage: 75.0, quality_gate_status: 'WARNING' as QualityGateStatus },
  { timestamp: '2025-01-24', total_issues: 462, bugs: 97, vulnerabilities: 14, code_smells: 351, coverage: 76.8, quality_gate_status: 'WARNING' as QualityGateStatus },
  { timestamp: '2025-01-25', total_issues: 455, bugs: 95, vulnerabilities: 13, code_smells: 347, coverage: 77.5, quality_gate_status: 'PASSED' as QualityGateStatus },
  { timestamp: '2025-01-26', total_issues: 450, bugs: 92, vulnerabilities: 12, code_smells: 346, coverage: 78.2, quality_gate_status: 'PASSED' as QualityGateStatus },
  { timestamp: '2025-01-27', total_issues: 448, bugs: 90, vulnerabilities: 12, code_smells: 346, coverage: 78.8, quality_gate_status: 'PASSED' as QualityGateStatus },
  { timestamp: '2025-01-28', total_issues: 445, bugs: 88, vulnerabilities: 11, code_smells: 346, coverage: 79.5, quality_gate_status: 'PASSED' as QualityGateStatus },
];

// Mock timeline events
const mockTimelineEvents: TimelineEvent[] = [
  {
    id: 'evt-1',
    timestamp: new Date(Date.now() - 3600000).toISOString(),
    event_type: 'vulnerability_found',
    severity: 'CRITICAL' as Severity,
    title: 'SQL Injection Vulnerability',
    description: 'Critical SQL injection found in user authentication endpoint',
    file_path: 'src/api/auth.py',
    rule_id: 'S001',
    project_name: 'codescope',
  },
  {
    id: 'evt-2',
    timestamp: new Date(Date.now() - 7200000).toISOString(),
    event_type: 'issue_fixed',
    severity: 'BLOCKER' as Severity,
    title: 'Command Injection Fixed',
    description: 'Fixed command injection vulnerability in CLI parser',
    file_path: 'src/cli/parser.py',
    rule_id: 'S002',
    project_name: 'codescope',
  },
  {
    id: 'evt-3',
    timestamp: new Date(Date.now() - 14400000).toISOString(),
    event_type: 'scan_complete',
    title: 'Daily Analysis Complete',
    description: 'Automated daily scan completed with 448 issues found',
    project_name: 'codescope',
  },
  {
    id: 'evt-4',
    timestamp: new Date(Date.now() - 28800000).toISOString(),
    event_type: 'quality_gate_change',
    title: 'Quality Gate Passed',
    description: 'Project now meets all quality gate conditions after fixes',
    project_name: 'codescope',
  },
  {
    id: 'evt-5',
    timestamp: new Date(Date.now() - 43200000).toISOString(),
    event_type: 'vulnerability_found',
    severity: 'MAJOR' as Severity,
    title: 'Path Traversal Issue',
    description: 'Path traversal vulnerability in file upload handler',
    file_path: 'src/api/uploads.py',
    rule_id: 'S015',
    project_name: 'codescope',
  },
  {
    id: 'evt-6',
    timestamp: new Date(Date.now() - 86400000).toISOString(),
    event_type: 'issue_created',
    severity: 'MINOR' as Severity,
    title: 'Unused Import',
    description: 'Detected 15 new unused import statements',
    project_name: 'codescope',
  },
  {
    id: 'evt-7',
    timestamp: new Date(Date.now() - 172800000).toISOString(),
    event_type: 'issue_fixed',
    severity: 'CRITICAL' as Severity,
    title: 'Hardcoded Credentials Removed',
    description: 'Removed hardcoded API keys from configuration file',
    file_path: 'src/config/settings.py',
    rule_id: 'S003',
    project_name: 'codescope',
  },
];

// Mock hotspot data
const mockHotspotData: HotspotData = {
  files: [
    { file_path: 'src/api/handlers/user.py', risk_score: 92, issues_count: 22, complexity: 48, churn: 28, authors: ['alice', 'bob', 'charlie', 'david'], last_modified: '2025-01-28T10:00:00Z' },
    { file_path: 'src/core/auth/session.py', risk_score: 87, issues_count: 18, complexity: 42, churn: 24, authors: ['alice', 'david'], last_modified: '2025-01-27T10:00:00Z' },
    { file_path: 'src/services/payment.py', risk_score: 82, issues_count: 15, complexity: 38, churn: 20, authors: ['bob', 'eve'], last_modified: '2025-01-26T10:00:00Z' },
    { file_path: 'src/analyzers/security.py', risk_score: 75, issues_count: 12, complexity: 35, churn: 18, authors: ['charlie', 'frank'], last_modified: '2025-01-25T10:00:00Z' },
    { file_path: 'src/parsers/python.py', risk_score: 68, issues_count: 10, complexity: 30, churn: 15, authors: ['david'], last_modified: '2025-01-24T10:00:00Z' },
    { file_path: 'src/utils/validators.py', risk_score: 58, issues_count: 8, complexity: 25, churn: 12, authors: ['eve', 'alice'], last_modified: '2025-01-23T10:00:00Z' },
    { file_path: 'src/models/issue.py', risk_score: 45, issues_count: 5, complexity: 18, churn: 8, authors: ['frank'], last_modified: '2025-01-22T10:00:00Z' },
    { file_path: 'src/reporters/json.py', risk_score: 32, issues_count: 3, complexity: 12, churn: 5, authors: ['bob', 'charlie'], last_modified: '2025-01-21T10:00:00Z' },
  ],
  directories: [
    { path: 'src/api/handlers', risk_score: 88, total_issues: 52, total_files: 15, average_complexity: 38.5 },
    { path: 'src/core/auth', risk_score: 78, total_issues: 35, total_files: 10, average_complexity: 32.2 },
    { path: 'src/services', risk_score: 65, total_issues: 28, total_files: 12, average_complexity: 26.8 },
    { path: 'src/analyzers', risk_score: 55, total_issues: 22, total_files: 8, average_complexity: 24.5 },
    { path: 'src/parsers', risk_score: 48, total_issues: 18, total_files: 6, average_complexity: 22.0 },
    { path: 'src/utils', risk_score: 35, total_issues: 12, total_files: 8, average_complexity: 16.5 },
  ],
};

const DeltaIndicator: React.FC<{ current: number; previous: number; inverse?: boolean }> = ({
  current,
  previous,
  inverse,
}) => {
  const diff = current - previous;
  if (diff === 0) return <span className="text-gray-400 text-sm">—</span>;
  const positive = inverse ? diff < 0 : diff > 0;
  return (
    <span className={`flex items-center text-sm ${positive ? 'text-green-600' : 'text-red-600'}`}>
      {diff > 0 ? (
        <ArrowTrendingUpIcon className="w-4 h-4 mr-1" />
      ) : (
        <ArrowTrendingDownIcon className="w-4 h-4 mr-1" />
      )}
      {diff > 0 ? '+' : ''}
      {typeof current === 'number' && current % 1 !== 0 ? diff.toFixed(1) : diff}
    </span>
  );
};

type TrendTab = 'charts' | 'timeline' | 'hotspots';

const Trends: React.FC = () => {
  const [project] = useState('codescope');
  const [activeTab, setActiveTab] = useState<TrendTab>('charts');
  const latest = mockSnapshots[mockSnapshots.length - 1];
  const prev = mockSnapshots[mockSnapshots.length - 2];

  const tabs = [
    { id: 'charts' as TrendTab, label: 'Trend Charts', icon: ChartBarIcon },
    { id: 'timeline' as TrendTab, label: 'Activity Timeline', icon: ClockIcon },
    { id: 'hotspots' as TrendTab, label: 'Hotspots', icon: FireIcon },
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Trend Analysis</h1>
          <p className="mt-1 text-sm text-gray-500">
            Track code quality and security improvements over time
          </p>
        </div>
        <span className="text-sm text-gray-500">
          {project} — {mockSnapshots.length} snapshots
        </span>
      </div>

      {/* Delta Cards */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
        {[
          { label: 'Total Issues', val: latest.total_issues, prev: prev.total_issues, inv: true },
          { label: 'Bugs', val: latest.bugs, prev: prev.bugs, inv: true },
          {
            label: 'Vulnerabilities',
            val: latest.vulnerabilities,
            prev: prev.vulnerabilities,
            inv: true,
          },
          { label: 'Code Smells', val: latest.code_smells, prev: prev.code_smells, inv: true },
          { label: 'Coverage', val: latest.coverage || 0, prev: prev.coverage || 0, inv: false },
        ].map(({ label, val, prev: p, inv }) => (
          <div key={label} className="bg-white rounded-lg border p-4">
            <p className="text-sm text-gray-500">{label}</p>
            <p className="text-2xl font-bold text-gray-900">
              {typeof val === 'number' && val % 1 !== 0 ? `${val}%` : val}
            </p>
            <DeltaIndicator current={val} previous={p} inverse={inv} />
          </div>
        ))}
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
      {activeTab === 'charts' && (
        <div className="space-y-6">
          {/* Issue Trends Chart */}
          <Card>
            <CardHeader
              title="Issue Trends"
              subtitle="Track the number of issues over time across all categories"
            />
            <CardBody>
              <TrendLineChart
                data={mockSnapshots}
                metrics={['total_issues', 'vulnerabilities', 'bugs', 'code_smells']}
                height={400}
                showQualityGate={true}
              />
            </CardBody>
          </Card>

          {/* Coverage Trend */}
          <Card>
            <CardHeader
              title="Coverage Trend"
              subtitle="Code coverage percentage progression"
            />
            <CardBody>
              <TrendLineChart
                data={mockSnapshots}
                metrics={['coverage']}
                height={250}
                showQualityGate={false}
              />
            </CardBody>
          </Card>

          {/* History Table */}
          <Card>
            <CardHeader title="Snapshot History" subtitle="Detailed metrics for each analysis snapshot" />
            <CardBody className="p-0">
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50">
                    <tr>
                      {['Date', 'Issues', 'Bugs', 'Vulns', 'Smells', 'Coverage', 'Gate'].map((h) => (
                        <th
                          key={h}
                          className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase"
                        >
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200">
                    {[...mockSnapshots].reverse().map((snap, i) => (
                      <tr key={i} className="hover:bg-gray-50">
                        <td className="px-4 py-3 text-sm">
                          {new Date(snap.timestamp).toLocaleDateString()}
                        </td>
                        <td className="px-4 py-3 text-sm font-medium">{snap.total_issues}</td>
                        <td className="px-4 py-3 text-sm">{snap.bugs}</td>
                        <td className="px-4 py-3 text-sm">{snap.vulnerabilities}</td>
                        <td className="px-4 py-3 text-sm">{snap.code_smells}</td>
                        <td className="px-4 py-3 text-sm">{snap.coverage}%</td>
                        <td className="px-4 py-3 text-sm">
                          <span
                            className={`px-2 py-1 rounded-full text-xs font-medium ${
                              snap.quality_gate_status === 'PASSED'
                                ? 'bg-green-100 text-green-800'
                                : snap.quality_gate_status === 'WARNING'
                                ? 'bg-yellow-100 text-yellow-800'
                                : 'bg-red-100 text-red-800'
                            }`}
                          >
                            {snap.quality_gate_status}
                          </span>
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

      {activeTab === 'timeline' && (
        <Card>
          <CardHeader
            title="Vulnerability Timeline"
            subtitle="Chronological view of security events and issue changes"
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
            subtitle="Files and directories with the highest risk scores based on issues, complexity, and churn"
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

export default Trends;
