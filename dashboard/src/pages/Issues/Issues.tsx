import React, { useState, useMemo } from 'react';
import {
  FunnelIcon,
  MagnifyingGlassIcon,
  ChevronDownIcon,
  ArrowDownTrayIcon,
} from '@heroicons/react/24/outline';
import {
  Card,
  CardHeader,
  CardBody,
  SeverityBadge,
  IssueTypeBadge,
  LoadingPage,
} from '../../components/common';
import { Issue, Severity, IssueType } from '../../types';
import { formatFilePath } from '../../utils/format';
import api from '../../services/api';

// Mock data
const mockIssues: Issue[] = [
  {
    id: '1',
    rule_id: 'python:S3649',
    rule_name: 'SQL Injection',
    severity: 'CRITICAL',
    issue_type: 'VULNERABILITY',
    message: 'User-controlled data used in SQL query without sanitization',
    location: { file_path: 'src/database/queries.py', start_line: 45, end_line: 47 },
    effort_minutes: 30,
    tags: ['security', 'owasp-a03', 'cwe-89'],
    snippet: 'cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")',
    suggestion: 'Use parameterized queries instead of string interpolation',
  },
  {
    id: '2',
    rule_id: 'python:S4790',
    rule_name: 'Command Injection',
    severity: 'BLOCKER',
    issue_type: 'VULNERABILITY',
    message: 'OS command executed with user-controlled input',
    location: { file_path: 'src/utils/shell.py', start_line: 23, end_line: 23 },
    effort_minutes: 45,
    tags: ['security', 'owasp-a03', 'cwe-78'],
    snippet: 'os.system(f"rm -rf {user_path}")',
  },
  {
    id: '3',
    rule_id: 'python:S1854',
    rule_name: 'Dead Store',
    severity: 'MINOR',
    issue_type: 'CODE_SMELL',
    message: 'Remove this useless assignment to local variable',
    location: { file_path: 'src/services/auth.py', start_line: 67, end_line: 67 },
    effort_minutes: 5,
    tags: ['code-smell', 'unused'],
  },
  {
    id: '4',
    rule_id: 'python:S1192',
    rule_name: 'String Literals Duplicated',
    severity: 'MINOR',
    issue_type: 'CODE_SMELL',
    message: 'Define a constant instead of duplicating this literal 5 times',
    location: { file_path: 'src/api/routes.py', start_line: 12, end_line: 12 },
    effort_minutes: 10,
    tags: ['code-smell', 'duplication'],
  },
  {
    id: '5',
    rule_id: 'python:S5445',
    rule_name: 'Insecure Temporary File',
    severity: 'MAJOR',
    issue_type: 'VULNERABILITY',
    message: 'Use a more secure method to create temporary files',
    location: { file_path: 'src/utils/files.py', start_line: 89, end_line: 89 },
    effort_minutes: 20,
    tags: ['security', 'cwe-377'],
  },
  {
    id: '6',
    rule_id: 'python:S134',
    rule_name: 'Control Flow Too Deep',
    severity: 'MAJOR',
    issue_type: 'CODE_SMELL',
    message: 'Refactor this code to reduce nesting level from 5 to 3',
    location: { file_path: 'src/core/processor.py', start_line: 156, end_line: 189 },
    effort_minutes: 60,
    tags: ['code-smell', 'complexity'],
  },
  {
    id: '7',
    rule_id: 'python:S2068',
    rule_name: 'Hardcoded Credentials',
    severity: 'CRITICAL',
    issue_type: 'VULNERABILITY',
    message: 'Remove this hardcoded password',
    location: { file_path: 'src/config/settings.py', start_line: 34, end_line: 34 },
    effort_minutes: 15,
    tags: ['security', 'owasp-a07', 'cwe-798'],
    snippet: 'DB_PASSWORD = "<CHANGE_ME>"',
  },
  {
    id: '8',
    rule_id: 'python:S1172',
    rule_name: 'Unused Parameter',
    severity: 'INFO',
    issue_type: 'CODE_SMELL',
    message: 'Remove unused parameter "options"',
    location: { file_path: 'src/handlers/events.py', start_line: 45, end_line: 45 },
    effort_minutes: 5,
    tags: ['code-smell', 'unused'],
  },
];

const SEVERITY_OPTIONS: Severity[] = ['BLOCKER', 'CRITICAL', 'MAJOR', 'MINOR', 'INFO'];
const TYPE_OPTIONS: IssueType[] = ['BUG', 'VULNERABILITY', 'CODE_SMELL', 'SECURITY_HOTSPOT'];

const Issues: React.FC = () => {
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedSeverities, setSelectedSeverities] = useState<Severity[]>([]);
  const [selectedTypes, setSelectedTypes] = useState<IssueType[]>([]);
  const [expandedIssue, setExpandedIssue] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  const handleExportPdf = async () => {
    setExporting(true);
    try {
      // Use the most recent analysis — in a real app this would come from context/route params
      const blob = await api.exportPdf('latest');
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'codescope-issues-report.pdf';
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

  const filteredIssues = useMemo(() => {
    return mockIssues.filter((issue) => {
      const matchesSearch =
        searchQuery === '' ||
        issue.message.toLowerCase().includes(searchQuery.toLowerCase()) ||
        issue.rule_name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        issue.location.file_path.toLowerCase().includes(searchQuery.toLowerCase());

      const matchesSeverity =
        selectedSeverities.length === 0 || selectedSeverities.includes(issue.severity);

      const matchesType =
        selectedTypes.length === 0 || selectedTypes.includes(issue.issue_type);

      return matchesSearch && matchesSeverity && matchesType;
    });
  }, [searchQuery, selectedSeverities, selectedTypes]);

  const toggleSeverity = (severity: Severity) => {
    setSelectedSeverities((prev) =>
      prev.includes(severity)
        ? prev.filter((s) => s !== severity)
        : [...prev, severity]
    );
  };

  const toggleType = (type: IssueType) => {
    setSelectedTypes((prev) =>
      prev.includes(type) ? prev.filter((t) => t !== type) : [...prev, type]
    );
  };

  const clearFilters = () => {
    setSelectedSeverities([]);
    setSelectedTypes([]);
    setSearchQuery('');
  };

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Issues</h1>
          <p className="mt-1 text-sm text-gray-500">
            Browse and filter all detected issues
          </p>
        </div>
        <button
          onClick={handleExportPdf}
          disabled={exporting}
          className="inline-flex items-center gap-2 px-4 py-2 bg-primary-600 text-white text-sm font-medium rounded-lg hover:bg-primary-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          <ArrowDownTrayIcon className="w-4 h-4" />
          {exporting ? 'Exporting...' : 'Export PDF'}
        </button>
      </div>

      {/* Filters */}
      <Card>
        <CardBody>
          <div className="flex flex-col lg:flex-row lg:items-center gap-4">
            {/* Search */}
            <div className="relative flex-1">
              <MagnifyingGlassIcon className="absolute left-3 top-1/2 transform -translate-y-1/2 w-5 h-5 text-gray-400" />
              <input
                type="text"
                placeholder="Search issues..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent"
              />
            </div>

            {/* Severity Filter */}
            <div className="flex flex-wrap gap-2">
              {SEVERITY_OPTIONS.map((severity) => (
                <button
                  key={severity}
                  onClick={() => toggleSeverity(severity)}
                  className={`px-3 py-1.5 text-xs font-medium rounded-full transition-colors ${
                    selectedSeverities.includes(severity)
                      ? 'bg-primary-600 text-white'
                      : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
                  }`}
                >
                  {severity}
                </button>
              ))}
            </div>

            {/* Type Filter */}
            <div className="flex flex-wrap gap-2">
              {TYPE_OPTIONS.map((type) => (
                <button
                  key={type}
                  onClick={() => toggleType(type)}
                  className={`px-3 py-1.5 text-xs font-medium rounded-full transition-colors ${
                    selectedTypes.includes(type)
                      ? 'bg-primary-600 text-white'
                      : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
                  }`}
                >
                  {type.replace('_', ' ')}
                </button>
              ))}
            </div>

            {/* Clear Filters */}
            {(selectedSeverities.length > 0 || selectedTypes.length > 0 || searchQuery) && (
              <button
                onClick={clearFilters}
                className="text-sm text-primary-600 hover:text-primary-700"
              >
                Clear filters
              </button>
            )}
          </div>
        </CardBody>
      </Card>

      {/* Issues List */}
      <Card>
        <CardHeader
          title={`${filteredIssues.length} Issues`}
          subtitle="Click on an issue to see details"
        />
        <CardBody className="p-0">
          <div className="divide-y divide-gray-200">
            {filteredIssues.map((issue) => (
              <div key={issue.id} className="hover:bg-gray-50">
                <div
                  className="px-6 py-4 cursor-pointer"
                  onClick={() =>
                    setExpandedIssue(expandedIssue === issue.id ? null : issue.id)
                  }
                >
                  <div className="flex items-start justify-between">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1">
                        <SeverityBadge severity={issue.severity} />
                        <IssueTypeBadge type={issue.issue_type} />
                        <span className="text-xs text-gray-500">{issue.rule_id}</span>
                      </div>
                      <p className="text-sm font-medium text-gray-900">
                        {issue.message}
                      </p>
                      <p className="text-sm text-gray-500 mt-1">
                        {formatFilePath(issue.location.file_path)} : {issue.location.start_line}
                      </p>
                    </div>
                    <ChevronDownIcon
                      className={`w-5 h-5 text-gray-400 transition-transform ${
                        expandedIssue === issue.id ? 'rotate-180' : ''
                      }`}
                    />
                  </div>
                </div>

                {/* Expanded Details */}
                {expandedIssue === issue.id && (
                  <div className="px-6 pb-4 bg-gray-50 border-t border-gray-100">
                    <div className="pt-4 space-y-4">
                      {/* Rule Info */}
                      <div>
                        <h4 className="text-xs font-medium text-gray-500 uppercase">
                          Rule
                        </h4>
                        <p className="mt-1 text-sm text-gray-900">{issue.rule_name}</p>
                      </div>

                      {/* Code Snippet */}
                      {issue.snippet && (
                        <div>
                          <h4 className="text-xs font-medium text-gray-500 uppercase">
                            Code
                          </h4>
                          <pre className="mt-1 p-3 bg-gray-800 text-gray-100 rounded-lg text-sm overflow-x-auto">
                            <code>{issue.snippet}</code>
                          </pre>
                        </div>
                      )}

                      {/* Suggestion */}
                      {issue.suggestion && (
                        <div>
                          <h4 className="text-xs font-medium text-gray-500 uppercase">
                            Suggestion
                          </h4>
                          <p className="mt-1 text-sm text-gray-700">{issue.suggestion}</p>
                        </div>
                      )}

                      {/* Tags */}
                      {issue.tags.length > 0 && (
                        <div>
                          <h4 className="text-xs font-medium text-gray-500 uppercase">
                            Tags
                          </h4>
                          <div className="mt-1 flex flex-wrap gap-1">
                            {issue.tags.map((tag) => (
                              <span
                                key={tag}
                                className="px-2 py-0.5 bg-gray-200 text-gray-700 text-xs rounded"
                              >
                                {tag}
                              </span>
                            ))}
                          </div>
                        </div>
                      )}

                      {/* Effort */}
                      {issue.effort_minutes && (
                        <div>
                          <h4 className="text-xs font-medium text-gray-500 uppercase">
                            Estimated Fix Time
                          </h4>
                          <p className="mt-1 text-sm text-gray-900">
                            {issue.effort_minutes} minutes
                          </p>
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            ))}

            {filteredIssues.length === 0 && (
              <div className="px-6 py-12 text-center text-gray-500">
                No issues found matching your filters
              </div>
            )}
          </div>
        </CardBody>
      </Card>
    </div>
  );
};

export default Issues;
