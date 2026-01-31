import React, { useState } from 'react';
import { ArrowTrendingUpIcon, ArrowTrendingDownIcon } from '@heroicons/react/24/outline';

const mockSnapshots = [
  { timestamp: '2025-01-25', total_issues: 520, bugs: 110, vulnerabilities: 18, code_smells: 392, coverage: 72.1, quality_gate: 'failed' },
  { timestamp: '2025-01-26', total_issues: 505, bugs: 105, vulnerabilities: 16, code_smells: 384, coverage: 73.5, quality_gate: 'failed' },
  { timestamp: '2025-01-27', total_issues: 488, bugs: 100, vulnerabilities: 14, code_smells: 374, coverage: 75.0, quality_gate: 'failed' },
  { timestamp: '2025-01-28', total_issues: 470, bugs: 95, vulnerabilities: 12, code_smells: 363, coverage: 77.2, quality_gate: 'passed' },
  { timestamp: '2025-01-29', total_issues: 455, bugs: 90, vulnerabilities: 10, code_smells: 355, coverage: 78.8, quality_gate: 'passed' },
  { timestamp: '2025-01-30', total_issues: 448, bugs: 88, vulnerabilities: 10, code_smells: 350, coverage: 80.1, quality_gate: 'passed' },
];

const DeltaIndicator: React.FC<{ current: number; previous: number; inverse?: boolean }> = ({ current, previous, inverse }) => {
  const diff = current - previous;
  if (diff === 0) return <span className="text-gray-400 text-sm">—</span>;
  const positive = inverse ? diff < 0 : diff > 0;
  return (
    <span className={`flex items-center text-sm ${positive ? 'text-green-600' : 'text-red-600'}`}>
      {diff > 0 ? <ArrowTrendingUpIcon className="w-4 h-4 mr-1" /> : <ArrowTrendingDownIcon className="w-4 h-4 mr-1" />}
      {diff > 0 ? '+' : ''}{diff}
    </span>
  );
};

const Trends: React.FC = () => {
  const [project] = useState('codescope');
  const latest = mockSnapshots[mockSnapshots.length - 1];
  const prev = mockSnapshots[mockSnapshots.length - 2];
  const maxIssues = Math.max(...mockSnapshots.map(s => s.total_issues));

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-900">Trend Analysis</h1>
        <span className="text-sm text-gray-500">{project} — {mockSnapshots.length} snapshots</span>
      </div>

      {/* Delta Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {[
          { label: 'Total Issues', val: latest.total_issues, prev: prev.total_issues, inv: true },
          { label: 'Bugs', val: latest.bugs, prev: prev.bugs, inv: true },
          { label: 'Vulnerabilities', val: latest.vulnerabilities, prev: prev.vulnerabilities, inv: true },
          { label: 'Coverage', val: latest.coverage, prev: prev.coverage, inv: false },
        ].map(({ label, val, prev: p, inv }) => (
          <div key={label} className="bg-white rounded-lg border p-4">
            <p className="text-sm text-gray-500">{label}</p>
            <p className="text-2xl font-bold text-gray-900">{typeof val === 'number' && val % 1 ? `${val}%` : val}</p>
            <DeltaIndicator current={val} previous={p} inverse={inv} />
          </div>
        ))}
      </div>

      {/* Simple Bar Chart */}
      <div className="bg-white rounded-lg border p-6">
        <h2 className="text-lg font-semibold text-gray-900 mb-4">Issues Over Time</h2>
        <div className="flex items-end space-x-2 h-48">
          {mockSnapshots.map((snap, i) => (
            <div key={i} className="flex-1 flex flex-col items-center">
              <div
                className={`w-full rounded-t ${snap.quality_gate === 'passed' ? 'bg-green-500' : 'bg-red-400'}`}
                style={{ height: `${(snap.total_issues / maxIssues) * 100}%` }}
                title={`${snap.total_issues} issues`}
              />
              <span className="text-xs text-gray-500 mt-1">{snap.timestamp.slice(5)}</span>
            </div>
          ))}
        </div>
      </div>

      {/* History Table */}
      <div className="bg-white rounded-lg border overflow-hidden">
        <table className="min-w-full divide-y divide-gray-200">
          <thead className="bg-gray-50">
            <tr>
              {['Date', 'Issues', 'Bugs', 'Vulns', 'Smells', 'Coverage', 'Gate'].map(h => (
                <th key={h} className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200">
            {[...mockSnapshots].reverse().map((snap, i) => (
              <tr key={i} className="hover:bg-gray-50">
                <td className="px-4 py-3 text-sm">{snap.timestamp}</td>
                <td className="px-4 py-3 text-sm font-medium">{snap.total_issues}</td>
                <td className="px-4 py-3 text-sm">{snap.bugs}</td>
                <td className="px-4 py-3 text-sm">{snap.vulnerabilities}</td>
                <td className="px-4 py-3 text-sm">{snap.code_smells}</td>
                <td className="px-4 py-3 text-sm">{snap.coverage}%</td>
                <td className="px-4 py-3 text-sm">
                  <span className={`px-2 py-1 rounded-full text-xs font-medium ${snap.quality_gate === 'passed' ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'}`}>
                    {snap.quality_gate}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};

export default Trends;
