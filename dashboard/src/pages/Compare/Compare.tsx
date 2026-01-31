import React from 'react';
import { ArrowsRightLeftIcon, PlusCircleIcon, MinusCircleIcon } from '@heroicons/react/24/outline';

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

const sevColor: Record<string, string> = {
  BLOCKER: 'bg-red-100 text-red-800',
  MAJOR: 'bg-orange-100 text-orange-800',
  MINOR: 'bg-yellow-100 text-yellow-800',
  INFO: 'bg-blue-100 text-blue-800',
};

const Compare: React.FC = () => {
  const { summary } = mockComparison;

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <ArrowsRightLeftIcon className="w-7 h-7 text-primary-600" />
        <h1 className="text-2xl font-bold text-gray-900">PR Diff Analysis</h1>
      </div>

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
      <div className="bg-white rounded-lg border overflow-hidden">
        <div className="px-4 py-3 bg-red-50 border-b">
          <h2 className="font-semibold text-red-800">New Issues ({mockComparison.new_issues.length} shown)</h2>
        </div>
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

      {/* Fixed Issues */}
      <div className="bg-white rounded-lg border overflow-hidden">
        <div className="px-4 py-3 bg-green-50 border-b">
          <h2 className="font-semibold text-green-800">Fixed Issues ({mockComparison.fixed_issues.length} shown)</h2>
        </div>
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
    </div>
  );
};

export default Compare;
