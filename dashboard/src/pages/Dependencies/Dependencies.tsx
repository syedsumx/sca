import React, { useState } from 'react';
import {
  CubeIcon,
  ShieldExclamationIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  MagnifyingGlassIcon,
} from '@heroicons/react/24/outline';
import {
  Card,
  CardHeader,
  CardBody,
  MetricCard,
  SeverityBadge,
} from '../../components/common';
import { Dependency, Vulnerability, Severity } from '../../types';
import { formatNumber } from '../../utils/format';

// Mock data
const mockDependencies: Dependency[] = [
  { name: 'react', version: '18.2.0', ecosystem: 'npm', source_file: 'package.json', is_dev: false },
  { name: 'axios', version: '1.4.0', ecosystem: 'npm', source_file: 'package.json', is_dev: false },
  { name: 'lodash', version: '4.17.21', ecosystem: 'npm', source_file: 'package.json', is_dev: false },
  { name: 'express', version: '4.18.2', ecosystem: 'npm', source_file: 'package.json', is_dev: false },
  { name: 'typescript', version: '5.0.4', ecosystem: 'npm', source_file: 'package.json', is_dev: true },
  { name: 'jest', version: '29.5.0', ecosystem: 'npm', source_file: 'package.json', is_dev: true },
  { name: 'requests', version: '2.28.0', ecosystem: 'pypi', source_file: 'requirements.txt', is_dev: false },
  { name: 'django', version: '4.1.7', ecosystem: 'pypi', source_file: 'requirements.txt', is_dev: false },
  { name: 'cryptography', version: '3.4.6', ecosystem: 'pypi', source_file: 'requirements.txt', is_dev: false },
  { name: 'pytest', version: '7.3.1', ecosystem: 'pypi', source_file: 'requirements.txt', is_dev: true },
];

const mockVulnerabilities: Vulnerability[] = [
  {
    id: 'CVE-2023-1234',
    severity: 'CRITICAL',
    summary: 'Remote code execution vulnerability in cryptography package',
    details: 'A vulnerability in the cryptography package allows remote attackers to execute arbitrary code via a crafted certificate.',
    affected_package: 'cryptography',
    affected_versions: '<39.0.1',
    fixed_version: '39.0.1',
    references: ['https://nvd.nist.gov/vuln/detail/CVE-2023-1234'],
    cvss_score: 9.8,
  },
  {
    id: 'CVE-2023-5678',
    severity: 'MAJOR',
    summary: 'SQL injection vulnerability in Django ORM',
    details: 'Django ORM is vulnerable to SQL injection when using raw queries with user input.',
    affected_package: 'django',
    affected_versions: '<4.2.0',
    fixed_version: '4.2.0',
    references: ['https://nvd.nist.gov/vuln/detail/CVE-2023-5678'],
    cvss_score: 7.5,
  },
  {
    id: 'CVE-2022-9012',
    severity: 'MINOR',
    summary: 'Denial of service in lodash debounce function',
    details: 'The debounce function in lodash can be exploited to cause excessive memory consumption.',
    affected_package: 'lodash',
    affected_versions: '<4.17.20',
    fixed_version: '4.17.20',
    references: ['https://nvd.nist.gov/vuln/detail/CVE-2022-9012'],
    cvss_score: 5.3,
  },
];

const Dependencies: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'all' | 'vulnerable'>('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedEcosystem, setSelectedEcosystem] = useState<string>('all');

  const ecosystems = [...new Set(mockDependencies.map((d) => d.ecosystem))];

  const filteredDependencies = mockDependencies.filter((dep) => {
    const matchesSearch = searchQuery === '' ||
      dep.name.toLowerCase().includes(searchQuery.toLowerCase());
    const matchesEcosystem = selectedEcosystem === 'all' || dep.ecosystem === selectedEcosystem;
    const matchesTab = activeTab === 'all' ||
      mockVulnerabilities.some((v) => v.affected_package === dep.name);
    return matchesSearch && matchesEcosystem && matchesTab;
  });

  const getVulnerabilityForDep = (depName: string) =>
    mockVulnerabilities.find((v) => v.affected_package === depName);

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Dependencies</h1>
        <p className="mt-1 text-sm text-gray-500">
          Manage third-party dependencies and security vulnerabilities
        </p>
      </div>

      {/* Summary Metrics */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <MetricCard
          title="Total Dependencies"
          value={mockDependencies.length}
          subtitle="packages detected"
          icon={<CubeIcon className="w-6 h-6" />}
        />
        <MetricCard
          title="Vulnerable"
          value={mockVulnerabilities.length}
          subtitle="packages with issues"
          icon={<ShieldExclamationIcon className="w-6 h-6" />}
        />
        <MetricCard
          title="Production"
          value={mockDependencies.filter((d) => !d.is_dev).length}
          subtitle="runtime dependencies"
        />
        <MetricCard
          title="Development"
          value={mockDependencies.filter((d) => d.is_dev).length}
          subtitle="dev dependencies"
        />
      </div>

      {/* Vulnerabilities Alert */}
      {mockVulnerabilities.length > 0 && (
        <Card className="border-l-4 border-red-500">
          <CardBody>
            <div className="flex items-start">
              <ExclamationTriangleIcon className="w-6 h-6 text-red-500 flex-shrink-0" />
              <div className="ml-3">
                <h3 className="text-sm font-medium text-red-800">
                  {mockVulnerabilities.length} Security Vulnerabilities Detected
                </h3>
                <p className="mt-1 text-sm text-red-700">
                  {mockVulnerabilities.filter((v) => v.severity === 'CRITICAL' || v.severity === 'BLOCKER').length} critical,{' '}
                  {mockVulnerabilities.filter((v) => v.severity === 'MAJOR').length} high severity issues found.
                  Review and update affected packages.
                </p>
              </div>
            </div>
          </CardBody>
        </Card>
      )}

      {/* Filters */}
      <Card>
        <CardBody>
          <div className="flex flex-col lg:flex-row lg:items-center gap-4">
            {/* Tabs */}
            <div className="flex space-x-1 bg-gray-100 p-1 rounded-lg">
              <button
                onClick={() => setActiveTab('all')}
                className={`px-4 py-2 text-sm font-medium rounded-md transition-colors ${
                  activeTab === 'all'
                    ? 'bg-white text-gray-900 shadow'
                    : 'text-gray-600 hover:text-gray-900'
                }`}
              >
                All ({mockDependencies.length})
              </button>
              <button
                onClick={() => setActiveTab('vulnerable')}
                className={`px-4 py-2 text-sm font-medium rounded-md transition-colors ${
                  activeTab === 'vulnerable'
                    ? 'bg-white text-gray-900 shadow'
                    : 'text-gray-600 hover:text-gray-900'
                }`}
              >
                Vulnerable ({mockVulnerabilities.length})
              </button>
            </div>

            {/* Search */}
            <div className="relative flex-1">
              <MagnifyingGlassIcon className="absolute left-3 top-1/2 transform -translate-y-1/2 w-5 h-5 text-gray-400" />
              <input
                type="text"
                placeholder="Search dependencies..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
              />
            </div>

            {/* Ecosystem Filter */}
            <select
              value={selectedEcosystem}
              onChange={(e) => setSelectedEcosystem(e.target.value)}
              className="px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
            >
              <option value="all">All Ecosystems</option>
              {ecosystems.map((eco) => (
                <option key={eco} value={eco}>{eco}</option>
              ))}
            </select>
          </div>
        </CardBody>
      </Card>

      {/* Dependencies List */}
      <Card>
        <CardHeader title="Dependencies" subtitle={`${filteredDependencies.length} packages`} />
        <CardBody className="p-0">
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Package</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Version</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Ecosystem</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200">
                {filteredDependencies.map((dep) => {
                  const vuln = getVulnerabilityForDep(dep.name);
                  return (
                    <tr key={`${dep.ecosystem}-${dep.name}`} className="hover:bg-gray-50">
                      <td className="px-6 py-4">
                        <div className="flex items-center">
                          <CubeIcon className="w-5 h-5 text-gray-400 mr-2" />
                          <span className="font-medium text-gray-900">{dep.name}</span>
                        </div>
                      </td>
                      <td className="px-6 py-4 text-sm text-gray-900">{dep.version}</td>
                      <td className="px-6 py-4">
                        <span className="px-2 py-1 text-xs font-medium bg-gray-100 text-gray-700 rounded">
                          {dep.ecosystem}
                        </span>
                      </td>
                      <td className="px-6 py-4">
                        <span className={`px-2 py-1 text-xs font-medium rounded ${
                          dep.is_dev ? 'bg-purple-100 text-purple-700' : 'bg-blue-100 text-blue-700'
                        }`}>
                          {dep.is_dev ? 'Dev' : 'Prod'}
                        </span>
                      </td>
                      <td className="px-6 py-4">
                        {vuln ? (
                          <div className="flex items-center">
                            <SeverityBadge severity={vuln.severity as Severity} />
                          </div>
                        ) : (
                          <div className="flex items-center text-green-600">
                            <CheckCircleIcon className="w-5 h-5 mr-1" />
                            <span className="text-sm">Secure</span>
                          </div>
                        )}
                      </td>
                      <td className="px-6 py-4">
                        {vuln && (
                          <button className="text-sm text-primary-600 hover:text-primary-700">
                            Update to {vuln.fixed_version}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </CardBody>
      </Card>
    </div>
  );
};

export default Dependencies;
