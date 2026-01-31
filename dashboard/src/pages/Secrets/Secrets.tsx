import React, { useState } from 'react';
import { KeyIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';

const mockFindings = [
  { rule_id: 'aws-access-key', rule_name: 'AWS Access Key', severity: 'CRITICAL', file: 'config/deploy.py', line: 42, matched_text: 'AKIA3T7X***_KEY', entropy: 4.2 },
  { rule_id: 'generic-secret', rule_name: 'Generic Secret', severity: 'MAJOR', file: 'src/database.py', line: 15, matched_text: 'password***rd"', entropy: 3.8 },
  { rule_id: 'github-token', rule_name: 'GitHub Token', severity: 'CRITICAL', file: '.env.example', line: 3, matched_text: 'ghp_xxxx***xxxx', entropy: 4.5 },
  { rule_id: 'private-key', rule_name: 'Private Key', severity: 'CRITICAL', file: 'certs/dev.pem', line: 1, matched_text: '-----BEG***KEY-----', entropy: 5.1 },
  { rule_id: 'database-url', rule_name: 'Database URL with Credentials', severity: 'CRITICAL', file: 'docker-compose.yml', line: 12, matched_text: 'postgres:***@db:5432', entropy: 3.9 },
];

const sevColor: Record<string, string> = {
  CRITICAL: 'bg-red-100 text-red-800',
  MAJOR: 'bg-orange-100 text-orange-800',
  MINOR: 'bg-yellow-100 text-yellow-800',
};

const Secrets: React.FC = () => {
  const [scanning, setScanning] = useState(false);
  const critical = mockFindings.filter(f => f.severity === 'CRITICAL').length;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-900">Secret Scanning</h1>
        <button
          onClick={() => { setScanning(true); setTimeout(() => setScanning(false), 2000); }}
          className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50"
          disabled={scanning}
        >
          {scanning ? 'Scanning...' : 'Run Secret Scan'}
        </button>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-3 gap-4">
        <div className="bg-white rounded-lg border p-4">
          <div className="flex items-center">
            <KeyIcon className="w-8 h-8 text-red-500" />
            <div className="ml-3">
              <p className="text-sm text-gray-500">Total Secrets Found</p>
              <p className="text-2xl font-bold text-gray-900">{mockFindings.length}</p>
            </div>
          </div>
        </div>
        <div className="bg-white rounded-lg border p-4">
          <div className="flex items-center">
            <ExclamationTriangleIcon className="w-8 h-8 text-red-600" />
            <div className="ml-3">
              <p className="text-sm text-gray-500">Critical</p>
              <p className="text-2xl font-bold text-red-600">{critical}</p>
            </div>
          </div>
        </div>
        <div className="bg-white rounded-lg border p-4">
          <p className="text-sm text-gray-500">Unique Secret Types</p>
          <p className="text-2xl font-bold text-gray-900">{new Set(mockFindings.map(f => f.rule_id)).size}</p>
        </div>
      </div>

      {/* Findings Table */}
      <div className="bg-white rounded-lg border overflow-hidden">
        <table className="min-w-full divide-y divide-gray-200">
          <thead className="bg-gray-50">
            <tr>
              {['Severity', 'Type', 'File', 'Line', 'Match (redacted)', 'Entropy'].map(h => (
                <th key={h} className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200">
            {mockFindings.map((f, i) => (
              <tr key={i} className="hover:bg-gray-50">
                <td className="px-4 py-3"><span className={`px-2 py-1 rounded-full text-xs font-medium ${sevColor[f.severity] || ''}`}>{f.severity}</span></td>
                <td className="px-4 py-3 text-sm font-medium">{f.rule_name}</td>
                <td className="px-4 py-3 text-sm text-gray-600 font-mono">{f.file}</td>
                <td className="px-4 py-3 text-sm">{f.line}</td>
                <td className="px-4 py-3 text-sm font-mono text-gray-500">{f.matched_text}</td>
                <td className="px-4 py-3 text-sm">{f.entropy}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};

export default Secrets;
