import React, { useState } from 'react';
import {
  SparklesIcon,
  ShieldExclamationIcon,
  ExclamationTriangleIcon,
  MagnifyingGlassIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline';
import { Card, CardHeader, CardBody, LoadingPage } from '../../components/common';

interface AIFinding {
  pattern_id: string;
  name: string;
  category: string;
  severity: string;
  message: string;
  file_path: string;
  start_line: number;
  end_line: number;
  confidence: number;
}

interface AIVetResult {
  files_scanned: number;
  total_findings: number;
  risk_score: number;
  risk_level: string;
  summary: Record<string, number>;
  findings: AIFinding[];
}

// Mock data for demonstration
const mockResult: AIVetResult = {
  files_scanned: 142,
  total_findings: 18,
  risk_score: 34.5,
  risk_level: 'MEDIUM',
  summary: {
    placeholder: 3,
    hallucination: 2,
    security: 5,
    quality: 4,
    incomplete: 2,
    overengineered: 1,
    license_risk: 1,
  },
  findings: [
    { pattern_id: 'ai:hardcoded-secret', name: 'Hardcoded Secret or Key', category: 'security', severity: 'BLOCKER', message: 'Hardcoded secret detected — AI-generated code often embeds example credentials', file_path: 'src/config/auth.py', start_line: 45, end_line: 45, confidence: 0.9 },
    { pattern_id: 'ai:sql-string-format', name: 'SQL String Formatting', category: 'security', severity: 'BLOCKER', message: 'SQL query built via string formatting — use parameterized queries instead', file_path: 'src/db/queries.py', start_line: 23, end_line: 23, confidence: 0.9 },
    { pattern_id: 'ai:hallucinated-api', name: 'Non-Existent API Call', category: 'hallucination', severity: 'CRITICAL', message: 'Likely hallucinated API call — os.path.exists_or_create does not exist', file_path: 'src/utils/files.py', start_line: 12, end_line: 12, confidence: 0.95 },
    { pattern_id: 'ai:insecure-default', name: 'Insecure Default Configuration', category: 'security', severity: 'CRITICAL', message: 'Insecure default: DEBUG = True should not be in production', file_path: 'src/settings.py', start_line: 8, end_line: 8, confidence: 0.85 },
    { pattern_id: 'ai:placeholder-comment', name: 'Placeholder Comment', category: 'placeholder', severity: 'MAJOR', message: 'Placeholder comment: # TODO: implement actual authentication', file_path: 'src/auth/handler.py', start_line: 67, end_line: 67, confidence: 0.85 },
    { pattern_id: 'ai:truncated-code', name: 'Truncated Code Block', category: 'incomplete', severity: 'BLOCKER', message: 'Possibly truncated AI output: # ... rest of implementation', file_path: 'src/services/processor.py', start_line: 145, end_line: 145, confidence: 0.9 },
    { pattern_id: 'ai:bare-except', name: 'Bare Except Clause', category: 'quality', severity: 'MAJOR', message: 'Overly broad exception handling — common AI pattern that hides bugs', file_path: 'src/api/views.py', start_line: 89, end_line: 89, confidence: 0.8 },
    { pattern_id: 'ai:example-value', name: 'Example / Dummy Value', category: 'placeholder', severity: 'MAJOR', message: 'Possible dummy value left by AI: "your_api_key"', file_path: 'src/integrations/slack.py', start_line: 15, end_line: 15, confidence: 0.8 },
    { pattern_id: 'ai:copied-license-header', name: 'Copied License Header', category: 'license_risk', severity: 'CRITICAL', message: 'License header detected — AI may have copied code from open-source', file_path: 'src/utils/parser.py', start_line: 1, end_line: 3, confidence: 0.7 },
    { pattern_id: 'ai:verbose-boolean', name: 'Verbose Boolean Logic', category: 'quality', severity: 'MINOR', message: 'Verbose boolean comparison — use "if x" instead of "if x == True"', file_path: 'src/validators.py', start_line: 34, end_line: 34, confidence: 0.85 },
  ],
};

const categoryLabels: Record<string, { label: string; color: string; icon: string }> = {
  placeholder: { label: 'Placeholder / Stub', color: 'bg-yellow-100 text-yellow-800', icon: '📝' },
  hallucination: { label: 'Hallucinated API', color: 'bg-purple-100 text-purple-800', icon: '👻' },
  security: { label: 'Security Risk', color: 'bg-red-100 text-red-800', icon: '🔒' },
  quality: { label: 'Code Quality', color: 'bg-blue-100 text-blue-800', icon: '✨' },
  incomplete: { label: 'Incomplete Code', color: 'bg-orange-100 text-orange-800', icon: '⚠️' },
  overengineered: { label: 'Over-Engineered', color: 'bg-indigo-100 text-indigo-800', icon: '🏗️' },
  license_risk: { label: 'License Risk', color: 'bg-pink-100 text-pink-800', icon: '📜' },
};

const severityColors: Record<string, string> = {
  BLOCKER: 'bg-red-600 text-white',
  CRITICAL: 'bg-red-500 text-white',
  MAJOR: 'bg-yellow-500 text-white',
  MINOR: 'bg-blue-500 text-white',
  INFO: 'bg-gray-400 text-white',
};

const AIVetting: React.FC = () => {
  const [result, setResult] = useState<AIVetResult | null>(mockResult);
  const [loading, setLoading] = useState(false);
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');

  const handleRunScan = async () => {
    setLoading(true);
    // In production, this would call the API:
    // const res = await api.post('/ai-vet', { path: '.' });
    // setResult(res.data);
    setTimeout(() => {
      setResult(mockResult);
      setLoading(false);
    }, 1500);
  };

  if (loading) {
    return <LoadingPage message="Vetting code for AI patterns..." />;
  }

  const filteredFindings = result?.findings.filter((f) => {
    if (selectedCategory && f.category !== selectedCategory) return false;
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      return (
        f.message.toLowerCase().includes(q) ||
        f.file_path.toLowerCase().includes(q) ||
        f.name.toLowerCase().includes(q)
      );
    }
    return true;
  }) ?? [];

  const riskColor = result
    ? { NONE: 'text-green-600', LOW: 'text-blue-600', MEDIUM: 'text-yellow-600', HIGH: 'text-red-600' }[result.risk_level] || 'text-gray-600'
    : 'text-gray-600';

  const riskBg = result
    ? { NONE: 'bg-green-50 border-green-200', LOW: 'bg-blue-50 border-blue-200', MEDIUM: 'bg-yellow-50 border-yellow-200', HIGH: 'bg-red-50 border-red-200' }[result.risk_level] || ''
    : '';

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
            <SparklesIcon className="w-7 h-7 text-primary-600" />
            AI Code Vetting
          </h1>
          <p className="mt-1 text-sm text-gray-500">
            Detect issues commonly introduced by AI code generation tools
          </p>
        </div>
        <button
          onClick={handleRunScan}
          className="inline-flex items-center gap-2 px-4 py-2 bg-primary-600 text-white text-sm font-medium rounded-lg hover:bg-primary-700 transition-colors"
        >
          <ArrowPathIcon className="w-4 h-4" />
          Run AI Vet
        </button>
      </div>

      {result && (
        <>
          {/* Risk Score Banner */}
          <div className={`border rounded-xl p-6 ${riskBg}`}>
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-500">AI Risk Level</p>
                <p className={`text-3xl font-bold ${riskColor}`}>{result.risk_level}</p>
              </div>
              <div className="text-right">
                <p className="text-sm font-medium text-gray-500">Risk Score</p>
                <p className={`text-3xl font-bold ${riskColor}`}>{result.risk_score.toFixed(1)}<span className="text-base text-gray-400">/100</span></p>
              </div>
              <div className="text-right">
                <p className="text-sm font-medium text-gray-500">Files Scanned</p>
                <p className="text-3xl font-bold text-gray-700">{result.files_scanned}</p>
              </div>
              <div className="text-right">
                <p className="text-sm font-medium text-gray-500">Findings</p>
                <p className="text-3xl font-bold text-gray-700">{result.total_findings}</p>
              </div>
            </div>
          </div>

          {/* Category Cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-3">
            {Object.entries(categoryLabels).map(([key, { label, color }]) => {
              const count = result.summary[key] || 0;
              const isActive = selectedCategory === key;
              return (
                <button
                  key={key}
                  onClick={() => setSelectedCategory(isActive ? null : key)}
                  className={`p-3 rounded-lg border text-center transition-all ${
                    isActive
                      ? 'ring-2 ring-primary-500 border-primary-300 bg-primary-50'
                      : 'border-gray-200 bg-white hover:border-gray-300'
                  }`}
                >
                  <p className="text-2xl font-bold text-gray-900">{count}</p>
                  <p className="text-xs text-gray-500 mt-1">{label}</p>
                </button>
              );
            })}
          </div>

          {/* Search & Filters */}
          <div className="flex items-center gap-4">
            <div className="relative flex-1">
              <MagnifyingGlassIcon className="w-5 h-5 text-gray-400 absolute left-3 top-2.5" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search findings by message, file, or pattern..."
                className="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-primary-500"
              />
            </div>
            {(selectedCategory || searchQuery) && (
              <button
                onClick={() => { setSelectedCategory(null); setSearchQuery(''); }}
                className="text-sm text-gray-500 hover:text-gray-700"
              >
                Clear filters
              </button>
            )}
          </div>

          {/* Findings Table */}
          <Card>
            <CardHeader
              title={`Findings (${filteredFindings.length})`}
              subtitle={selectedCategory ? `Filtered by: ${categoryLabels[selectedCategory]?.label}` : 'All categories'}
            />
            <CardBody className="p-0">
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50">
                    <tr>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Severity</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Category</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Pattern</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">File</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Line</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Message</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Confidence</th>
                    </tr>
                  </thead>
                  <tbody className="bg-white divide-y divide-gray-200">
                    {filteredFindings.map((finding, idx) => {
                      const cat = categoryLabels[finding.category];
                      return (
                        <tr key={idx} className="hover:bg-gray-50">
                          <td className="px-4 py-3 whitespace-nowrap">
                            <span className={`inline-flex px-2 py-0.5 text-xs font-semibold rounded-full ${severityColors[finding.severity] || 'bg-gray-200'}`}>
                              {finding.severity}
                            </span>
                          </td>
                          <td className="px-4 py-3 whitespace-nowrap">
                            <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full ${cat?.color || 'bg-gray-100'}`}>
                              {cat?.label || finding.category}
                            </span>
                          </td>
                          <td className="px-4 py-3 whitespace-nowrap text-sm font-medium text-gray-900">
                            {finding.name}
                          </td>
                          <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-500 font-mono">
                            {finding.file_path.length > 35 ? '...' + finding.file_path.slice(-32) : finding.file_path}
                          </td>
                          <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-500">
                            {finding.start_line}
                          </td>
                          <td className="px-4 py-3 text-sm text-gray-600 max-w-xs truncate" title={finding.message}>
                            {finding.message}
                          </td>
                          <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-500">
                            {(finding.confidence * 100).toFixed(0)}%
                          </td>
                        </tr>
                      );
                    })}
                    {filteredFindings.length === 0 && (
                      <tr>
                        <td colSpan={7} className="px-4 py-8 text-center text-gray-500">
                          No findings match the current filters.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </CardBody>
          </Card>
        </>
      )}
    </div>
  );
};

export default AIVetting;
