import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  FolderIcon,
  ChartBarIcon,
  ShieldCheckIcon,
  ClockIcon,
} from '@heroicons/react/24/outline';
import {
  Card,
  CardHeader,
  CardBody,
  StatusBadge,
  QualityBadge,
  ProgressBar,
} from '../../components/common';
import { ProjectSummary, QualityGateStatus, QualityGrade } from '../../types';
import { formatNumber, formatRelativeTime } from '../../utils/format';

// Mock data
const mockProjects: ProjectSummary[] = [
  {
    project_name: 'my-web-app',
    last_analysis: new Date(Date.now() - 3600000).toISOString(),
    quality_gate_status: 'PASSED',
    bugs: 5,
    vulnerabilities: 2,
    code_smells: 45,
    coverage: 78.5,
    duplications: 3.2,
    lines_of_code: 15420,
    reliability_rating: 'B',
    security_rating: 'A',
    maintainability_rating: 'B',
  },
  {
    project_name: 'api-service',
    last_analysis: new Date(Date.now() - 7200000).toISOString(),
    quality_gate_status: 'FAILED',
    bugs: 12,
    vulnerabilities: 8,
    code_smells: 89,
    coverage: 45.2,
    duplications: 12.5,
    lines_of_code: 28340,
    reliability_rating: 'C',
    security_rating: 'D',
    maintainability_rating: 'C',
  },
  {
    project_name: 'mobile-app',
    last_analysis: new Date(Date.now() - 86400000).toISOString(),
    quality_gate_status: 'PASSED',
    bugs: 2,
    vulnerabilities: 0,
    code_smells: 23,
    coverage: 85.3,
    duplications: 1.8,
    lines_of_code: 8750,
    reliability_rating: 'A',
    security_rating: 'A',
    maintainability_rating: 'A',
  },
  {
    project_name: 'data-pipeline',
    last_analysis: new Date(Date.now() - 172800000).toISOString(),
    quality_gate_status: 'WARNING',
    bugs: 8,
    vulnerabilities: 3,
    code_smells: 56,
    coverage: 62.1,
    duplications: 5.4,
    lines_of_code: 12890,
    reliability_rating: 'B',
    security_rating: 'B',
    maintainability_rating: 'C',
  },
];

const Projects: React.FC = () => {
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid');

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Projects</h1>
          <p className="mt-1 text-sm text-gray-500">
            Manage and monitor your analyzed projects
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setViewMode('grid')}
            className={`p-2 rounded ${viewMode === 'grid' ? 'bg-primary-100 text-primary-600' : 'text-gray-400 hover:text-gray-600'}`}
          >
            <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
              <path d="M5 3a2 2 0 00-2 2v2a2 2 0 002 2h2a2 2 0 002-2V5a2 2 0 00-2-2H5zM5 11a2 2 0 00-2 2v2a2 2 0 002 2h2a2 2 0 002-2v-2a2 2 0 00-2-2H5zM11 5a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V5zM11 13a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" />
            </svg>
          </button>
          <button
            onClick={() => setViewMode('list')}
            className={`p-2 rounded ${viewMode === 'list' ? 'bg-primary-100 text-primary-600' : 'text-gray-400 hover:text-gray-600'}`}
          >
            <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
              <path fillRule="evenodd" d="M3 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1z" clipRule="evenodd" />
            </svg>
          </button>
        </div>
      </div>

      {/* Projects Grid */}
      {viewMode === 'grid' ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {mockProjects.map((project) => (
            <Link key={project.project_name} to={`/projects/${project.project_name}`}>
              <Card className="hover:shadow-md transition-shadow cursor-pointer h-full">
                <CardBody>
                  <div className="flex items-start justify-between mb-4">
                    <div className="flex items-center">
                      <div className="p-2 bg-primary-100 rounded-lg">
                        <FolderIcon className="w-6 h-6 text-primary-600" />
                      </div>
                      <div className="ml-3">
                        <h3 className="font-semibold text-gray-900">{project.project_name}</h3>
                        <p className="text-xs text-gray-500">
                          {formatNumber(project.lines_of_code)} lines
                        </p>
                      </div>
                    </div>
                    <StatusBadge status={project.quality_gate_status as QualityGateStatus} />
                  </div>

                  {/* Ratings */}
                  <div className="flex items-center gap-4 mb-4">
                    <div className="flex items-center gap-1">
                      <span className="text-xs text-gray-500">Reliability</span>
                      <QualityBadge grade={project.reliability_rating as QualityGrade} />
                    </div>
                    <div className="flex items-center gap-1">
                      <span className="text-xs text-gray-500">Security</span>
                      <QualityBadge grade={project.security_rating as QualityGrade} />
                    </div>
                    <div className="flex items-center gap-1">
                      <span className="text-xs text-gray-500">Maint.</span>
                      <QualityBadge grade={project.maintainability_rating as QualityGrade} />
                    </div>
                  </div>

                  {/* Metrics */}
                  <div className="grid grid-cols-3 gap-2 mb-4 text-center">
                    <div className="p-2 bg-gray-50 rounded">
                      <p className="text-lg font-semibold text-gray-900">{project.bugs}</p>
                      <p className="text-xs text-gray-500">Bugs</p>
                    </div>
                    <div className="p-2 bg-gray-50 rounded">
                      <p className="text-lg font-semibold text-gray-900">{project.vulnerabilities}</p>
                      <p className="text-xs text-gray-500">Vulns</p>
                    </div>
                    <div className="p-2 bg-gray-50 rounded">
                      <p className="text-lg font-semibold text-gray-900">{project.code_smells}</p>
                      <p className="text-xs text-gray-500">Smells</p>
                    </div>
                  </div>

                  {/* Coverage */}
                  {project.coverage !== undefined && (
                    <ProgressBar
                      value={project.coverage}
                      label="Coverage"
                      size="sm"
                      className="mb-2"
                    />
                  )}

                  {/* Last Analysis */}
                  <div className="flex items-center text-xs text-gray-500 mt-3">
                    <ClockIcon className="w-4 h-4 mr-1" />
                    {project.last_analysis
                      ? `Analyzed ${formatRelativeTime(project.last_analysis)}`
                      : 'Never analyzed'}
                  </div>
                </CardBody>
              </Card>
            </Link>
          ))}
        </div>
      ) : (
        /* Projects List */
        <Card>
          <CardBody className="p-0">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Project</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Bugs</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Vulns</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Smells</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Coverage</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Last Analysis</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200">
                {mockProjects.map((project) => (
                  <tr key={project.project_name} className="hover:bg-gray-50">
                    <td className="px-6 py-4">
                      <Link to={`/projects/${project.project_name}`} className="flex items-center">
                        <FolderIcon className="w-5 h-5 text-gray-400 mr-2" />
                        <span className="font-medium text-gray-900">{project.project_name}</span>
                      </Link>
                    </td>
                    <td className="px-6 py-4">
                      <StatusBadge status={project.quality_gate_status as QualityGateStatus} />
                    </td>
                    <td className="px-6 py-4 text-sm text-gray-900">{project.bugs}</td>
                    <td className="px-6 py-4 text-sm text-gray-900">{project.vulnerabilities}</td>
                    <td className="px-6 py-4 text-sm text-gray-900">{project.code_smells}</td>
                    <td className="px-6 py-4 text-sm text-gray-900">
                      {project.coverage !== undefined ? `${project.coverage.toFixed(1)}%` : '-'}
                    </td>
                    <td className="px-6 py-4 text-sm text-gray-500">
                      {project.last_analysis ? formatRelativeTime(project.last_analysis) : 'Never'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardBody>
        </Card>
      )}
    </div>
  );
};

export default Projects;
