import React, { useState, useMemo } from 'react';
import {
  BookOpenIcon,
  MagnifyingGlassIcon,
  TagIcon,
} from '@heroicons/react/24/outline';
import {
  Card,
  CardHeader,
  CardBody,
  SeverityBadge,
  IssueTypeBadge,
} from '../../components/common';
import { Severity, IssueType } from '../../types';

interface Rule {
  id: string;
  name: string;
  description: string;
  severity: Severity;
  type: IssueType;
  language: string;
  tags: string[];
  enabled: boolean;
}

// Mock rules data
const mockRules: Rule[] = [
  { id: 'python:S3649', name: 'SQL Injection', description: 'User-controlled data used in SQL queries should be sanitized', severity: 'CRITICAL', type: 'VULNERABILITY', language: 'Python', tags: ['security', 'owasp-a03', 'cwe-89'], enabled: true },
  { id: 'python:S4790', name: 'Command Injection', description: 'OS commands should not be constructed from user input', severity: 'BLOCKER', type: 'VULNERABILITY', language: 'Python', tags: ['security', 'owasp-a03', 'cwe-78'], enabled: true },
  { id: 'python:S2068', name: 'Hardcoded Credentials', description: 'Credentials should not be hard-coded', severity: 'CRITICAL', type: 'VULNERABILITY', language: 'Python', tags: ['security', 'owasp-a07', 'cwe-798'], enabled: true },
  { id: 'python:S1854', name: 'Dead Store', description: 'Remove useless assignments to local variables', severity: 'MINOR', type: 'CODE_SMELL', language: 'Python', tags: ['unused', 'clean-code'], enabled: true },
  { id: 'python:S134', name: 'Control Flow Too Deep', description: 'Reduce nesting level to improve readability', severity: 'MAJOR', type: 'CODE_SMELL', language: 'Python', tags: ['complexity', 'maintainability'], enabled: true },
  { id: 'javascript:S3649', name: 'SQL Injection', description: 'User-controlled data used in SQL queries should be sanitized', severity: 'CRITICAL', type: 'VULNERABILITY', language: 'JavaScript', tags: ['security', 'owasp-a03', 'cwe-89'], enabled: true },
  { id: 'javascript:S2245', name: 'Pseudorandom Number Generators', description: 'Using pseudorandom number generators in security context is insecure', severity: 'CRITICAL', type: 'VULNERABILITY', language: 'JavaScript', tags: ['security', 'cwe-338'], enabled: true },
  { id: 'javascript:S1481', name: 'Unused Local Variables', description: 'Remove unused local variables', severity: 'MINOR', type: 'CODE_SMELL', language: 'JavaScript', tags: ['unused', 'clean-code'], enabled: true },
  { id: 'java:S3649', name: 'SQL Injection', description: 'User-controlled data used in SQL queries should be sanitized', severity: 'CRITICAL', type: 'VULNERABILITY', language: 'Java', tags: ['security', 'owasp-a03', 'cwe-89'], enabled: true },
  { id: 'java:S2083', name: 'Path Traversal', description: 'User-controlled data should not be used in file paths', severity: 'CRITICAL', type: 'VULNERABILITY', language: 'Java', tags: ['security', 'owasp-a01', 'cwe-22'], enabled: true },
  { id: 'java:S1186', name: 'Empty Method Body', description: 'Methods should not have empty implementations', severity: 'MAJOR', type: 'CODE_SMELL', language: 'Java', tags: ['suspicious', 'clean-code'], enabled: false },
  { id: 'go:S1007', name: 'SQL Injection', description: 'User-controlled data used in SQL queries should be sanitized', severity: 'CRITICAL', type: 'VULNERABILITY', language: 'Go', tags: ['security', 'owasp-a03', 'cwe-89'], enabled: true },
  { id: 'go:S1008', name: 'Command Injection', description: 'OS commands should not be constructed from user input', severity: 'BLOCKER', type: 'VULNERABILITY', language: 'Go', tags: ['security', 'owasp-a03', 'cwe-78'], enabled: true },
];

const LANGUAGES = ['All', 'Python', 'JavaScript', 'Java', 'Go', 'C#', 'Ruby', 'PHP'];
const SEVERITY_OPTIONS: Severity[] = ['BLOCKER', 'CRITICAL', 'MAJOR', 'MINOR', 'INFO'];
const TYPE_OPTIONS: IssueType[] = ['BUG', 'VULNERABILITY', 'CODE_SMELL', 'SECURITY_HOTSPOT'];

const Rules: React.FC = () => {
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedLanguage, setSelectedLanguage] = useState('All');
  const [selectedSeverities, setSelectedSeverities] = useState<Severity[]>([]);
  const [selectedTypes, setSelectedTypes] = useState<IssueType[]>([]);
  const [showEnabledOnly, setShowEnabledOnly] = useState(false);

  const filteredRules = useMemo(() => {
    return mockRules.filter((rule) => {
      const matchesSearch = searchQuery === '' ||
        rule.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        rule.description.toLowerCase().includes(searchQuery.toLowerCase()) ||
        rule.id.toLowerCase().includes(searchQuery.toLowerCase());

      const matchesLanguage = selectedLanguage === 'All' || rule.language === selectedLanguage;
      const matchesSeverity = selectedSeverities.length === 0 || selectedSeverities.includes(rule.severity);
      const matchesType = selectedTypes.length === 0 || selectedTypes.includes(rule.type);
      const matchesEnabled = !showEnabledOnly || rule.enabled;

      return matchesSearch && matchesLanguage && matchesSeverity && matchesType && matchesEnabled;
    });
  }, [searchQuery, selectedLanguage, selectedSeverities, selectedTypes, showEnabledOnly]);

  const toggleSeverity = (severity: Severity) => {
    setSelectedSeverities((prev) =>
      prev.includes(severity) ? prev.filter((s) => s !== severity) : [...prev, severity]
    );
  };

  const toggleType = (type: IssueType) => {
    setSelectedTypes((prev) =>
      prev.includes(type) ? prev.filter((t) => t !== type) : [...prev, type]
    );
  };

  const rulesByLanguage = useMemo(() => {
    const counts: Record<string, number> = { All: mockRules.length };
    mockRules.forEach((rule) => {
      counts[rule.language] = (counts[rule.language] || 0) + 1;
    });
    return counts;
  }, []);

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Rules</h1>
        <p className="mt-1 text-sm text-gray-500">
          Configure analysis rules and quality profiles
        </p>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card>
          <CardBody className="text-center">
            <p className="text-3xl font-bold text-gray-900">{mockRules.length}</p>
            <p className="text-sm text-gray-500">Total Rules</p>
          </CardBody>
        </Card>
        <Card>
          <CardBody className="text-center">
            <p className="text-3xl font-bold text-green-600">{mockRules.filter((r) => r.enabled).length}</p>
            <p className="text-sm text-gray-500">Enabled</p>
          </CardBody>
        </Card>
        <Card>
          <CardBody className="text-center">
            <p className="text-3xl font-bold text-orange-600">
              {mockRules.filter((r) => r.type === 'VULNERABILITY').length}
            </p>
            <p className="text-sm text-gray-500">Security Rules</p>
          </CardBody>
        </Card>
        <Card>
          <CardBody className="text-center">
            <p className="text-3xl font-bold text-blue-600">
              {new Set(mockRules.map((r) => r.language)).size}
            </p>
            <p className="text-sm text-gray-500">Languages</p>
          </CardBody>
        </Card>
      </div>

      {/* Filters */}
      <Card>
        <CardBody>
          <div className="space-y-4">
            {/* Search and Language */}
            <div className="flex flex-col lg:flex-row gap-4">
              <div className="relative flex-1">
                <MagnifyingGlassIcon className="absolute left-3 top-1/2 transform -translate-y-1/2 w-5 h-5 text-gray-400" />
                <input
                  type="text"
                  placeholder="Search rules..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                />
              </div>

              <div className="flex flex-wrap gap-2">
                {LANGUAGES.map((lang) => (
                  <button
                    key={lang}
                    onClick={() => setSelectedLanguage(lang)}
                    className={`px-3 py-1.5 text-sm font-medium rounded-full transition-colors ${
                      selectedLanguage === lang
                        ? 'bg-primary-600 text-white'
                        : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
                    }`}
                  >
                    {lang} ({rulesByLanguage[lang] || 0})
                  </button>
                ))}
              </div>
            </div>

            {/* Severity and Type filters */}
            <div className="flex flex-wrap items-center gap-4">
              <span className="text-sm text-gray-500">Severity:</span>
              {SEVERITY_OPTIONS.map((severity) => (
                <button
                  key={severity}
                  onClick={() => toggleSeverity(severity)}
                  className={`px-2 py-1 text-xs font-medium rounded transition-colors ${
                    selectedSeverities.includes(severity)
                      ? 'bg-primary-600 text-white'
                      : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
                  }`}
                >
                  {severity}
                </button>
              ))}

              <span className="text-sm text-gray-500 ml-4">Type:</span>
              {TYPE_OPTIONS.map((type) => (
                <button
                  key={type}
                  onClick={() => toggleType(type)}
                  className={`px-2 py-1 text-xs font-medium rounded transition-colors ${
                    selectedTypes.includes(type)
                      ? 'bg-primary-600 text-white'
                      : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
                  }`}
                >
                  {type.replace('_', ' ')}
                </button>
              ))}

              <label className="flex items-center ml-auto text-sm">
                <input
                  type="checkbox"
                  checked={showEnabledOnly}
                  onChange={(e) => setShowEnabledOnly(e.target.checked)}
                  className="mr-2 rounded border-gray-300"
                />
                Enabled only
              </label>
            </div>
          </div>
        </CardBody>
      </Card>

      {/* Rules List */}
      <Card>
        <CardHeader title={`${filteredRules.length} Rules`} />
        <CardBody className="p-0">
          <div className="divide-y divide-gray-200">
            {filteredRules.map((rule) => (
              <div key={rule.id} className="px-6 py-4 hover:bg-gray-50">
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-1">
                      <SeverityBadge severity={rule.severity} />
                      <IssueTypeBadge type={rule.type} />
                      <span className="px-2 py-0.5 text-xs font-medium bg-gray-100 text-gray-600 rounded">
                        {rule.language}
                      </span>
                      <span className="text-xs text-gray-400">{rule.id}</span>
                    </div>
                    <h4 className="text-sm font-medium text-gray-900">{rule.name}</h4>
                    <p className="text-sm text-gray-500 mt-1">{rule.description}</p>
                    <div className="flex items-center gap-1 mt-2">
                      <TagIcon className="w-4 h-4 text-gray-400" />
                      {rule.tags.map((tag) => (
                        <span key={tag} className="px-1.5 py-0.5 text-xs bg-gray-100 text-gray-600 rounded">
                          {tag}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div className="ml-4">
                    <button
                      className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                        rule.enabled ? 'bg-primary-600' : 'bg-gray-200'
                      }`}
                    >
                      <span
                        className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                          rule.enabled ? 'translate-x-6' : 'translate-x-1'
                        }`}
                      />
                    </button>
                  </div>
                </div>
              </div>
            ))}

            {filteredRules.length === 0 && (
              <div className="px-6 py-12 text-center text-gray-500">
                No rules found matching your filters
              </div>
            )}
          </div>
        </CardBody>
      </Card>
    </div>
  );
};

export default Rules;
