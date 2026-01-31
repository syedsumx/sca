import React, { useState } from 'react';
import {
  ChartBarIcon,
  DocumentTextIcon,
  CheckCircleIcon,
  XCircleIcon,
} from '@heroicons/react/24/outline';
import {
  Card,
  CardHeader,
  CardBody,
  MetricCard,
  ProgressBar,
} from '../../components/common';
import { FileCoverage } from '../../types';
import { formatFilePath, formatPercentage } from '../../utils/format';

// Mock data
const mockCoverageResult = {
  line_coverage: 72.5,
  branch_coverage: 65.3,
  total_lines: 15420,
  covered_lines: 11180,
  uncovered_lines: 4240,
  files: [
    { file_path: 'src/core/processor.py', line_coverage: 95.2, branch_coverage: 88.5, covered_lines: 198, total_lines: 208, uncovered_lines: [45, 67, 89, 102, 115, 178, 189, 195, 201, 205] },
    { file_path: 'src/handlers/user_handler.py', line_coverage: 88.7, covered_lines: 156, total_lines: 176, uncovered_lines: [23, 45, 67, 89, 112, 134, 145, 156, 167, 170, 172, 174, 175, 176] },
    { file_path: 'src/services/auth.py', line_coverage: 82.3, covered_lines: 134, total_lines: 163, uncovered_lines: [12, 34, 56, 78, 90, 101, 112, 123, 134, 145, 150, 155, 158, 160, 161, 162, 163] },
    { file_path: 'src/api/routes.py', line_coverage: 76.5, covered_lines: 98, total_lines: 128, uncovered_lines: [15, 28, 41, 54, 67, 80, 93, 106, 110, 115, 118, 120, 122, 124, 125, 126, 127, 128] },
    { file_path: 'src/utils/validators.py', line_coverage: 65.4, covered_lines: 85, total_lines: 130, uncovered_lines: Array.from({ length: 45 }, (_, i) => 86 + i) },
    { file_path: 'src/models/user.py', line_coverage: 58.9, covered_lines: 56, total_lines: 95, uncovered_lines: Array.from({ length: 39 }, (_, i) => 57 + i) },
    { file_path: 'src/database/queries.py', line_coverage: 45.2, covered_lines: 42, total_lines: 93, uncovered_lines: Array.from({ length: 51 }, (_, i) => 43 + i) },
    { file_path: 'src/config/settings.py', line_coverage: 92.1, covered_lines: 35, total_lines: 38, uncovered_lines: [36, 37, 38] },
  ] as FileCoverage[],
};

const Coverage: React.FC = () => {
  const [sortBy, setSortBy] = useState<'name' | 'coverage'>('coverage');
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('asc');
  const [showUncoveredOnly, setShowUncoveredOnly] = useState(false);

  const data = mockCoverageResult;

  const sortedFiles = [...data.files]
    .filter((f) => !showUncoveredOnly || f.line_coverage < 80)
    .sort((a, b) => {
      if (sortBy === 'name') {
        return sortOrder === 'asc'
          ? a.file_path.localeCompare(b.file_path)
          : b.file_path.localeCompare(a.file_path);
      }
      return sortOrder === 'asc'
        ? a.line_coverage - b.line_coverage
        : b.line_coverage - a.line_coverage;
    });

  const getCoverageColor = (coverage: number) => {
    if (coverage >= 80) return 'text-green-600';
    if (coverage >= 60) return 'text-lime-600';
    if (coverage >= 40) return 'text-amber-600';
    return 'text-red-600';
  };

  const getCoverageStatus = (coverage: number) => {
    if (coverage >= 80) return { icon: CheckCircleIcon, color: 'text-green-500', label: 'Good' };
    if (coverage >= 60) return { icon: CheckCircleIcon, color: 'text-lime-500', label: 'Fair' };
    return { icon: XCircleIcon, color: 'text-red-500', label: 'Needs Improvement' };
  };

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Code Coverage</h1>
        <p className="mt-1 text-sm text-gray-500">
          Track test coverage across your codebase
        </p>
      </div>

      {/* Summary Metrics */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <MetricCard
          title="Line Coverage"
          value={`${data.line_coverage.toFixed(1)}%`}
          subtitle="of lines covered"
          icon={<ChartBarIcon className="w-6 h-6" />}
        />
        <MetricCard
          title="Branch Coverage"
          value={data.branch_coverage ? `${data.branch_coverage.toFixed(1)}%` : 'N/A'}
          subtitle="of branches covered"
        />
        <MetricCard
          title="Covered Lines"
          value={data.covered_lines.toLocaleString()}
          subtitle={`of ${data.total_lines.toLocaleString()} total`}
        />
        <MetricCard
          title="Uncovered Lines"
          value={data.uncovered_lines.toLocaleString()}
          subtitle="lines need tests"
        />
      </div>

      {/* Coverage Gauge */}
      <Card>
        <CardBody>
          <div className="flex items-center justify-between mb-4">
            <div>
              <h3 className="text-lg font-medium text-gray-900">Overall Coverage</h3>
              <p className="text-sm text-gray-500">Target: 80% minimum coverage</p>
            </div>
            <div className={`text-3xl font-bold ${getCoverageColor(data.line_coverage)}`}>
              {data.line_coverage.toFixed(1)}%
            </div>
          </div>
          <div className="relative">
            <ProgressBar
              value={data.line_coverage}
              showValue={false}
              size="lg"
            />
            {/* Target marker */}
            <div
              className="absolute top-0 bottom-0 w-0.5 bg-gray-800"
              style={{ left: '80%' }}
            >
              <span className="absolute -top-6 -translate-x-1/2 text-xs font-medium text-gray-600">
                80% target
              </span>
            </div>
          </div>
        </CardBody>
      </Card>

      {/* Files Coverage */}
      <Card>
        <CardHeader
          title="File Coverage"
          subtitle={`${sortedFiles.length} files`}
          action={
            <div className="flex items-center gap-4">
              <label className="flex items-center text-sm">
                <input
                  type="checkbox"
                  checked={showUncoveredOnly}
                  onChange={(e) => setShowUncoveredOnly(e.target.checked)}
                  className="mr-2 rounded border-gray-300"
                />
                Show low coverage only
              </label>
              <select
                value={`${sortBy}-${sortOrder}`}
                onChange={(e) => {
                  const [newSortBy, newSortOrder] = e.target.value.split('-') as ['name' | 'coverage', 'asc' | 'desc'];
                  setSortBy(newSortBy);
                  setSortOrder(newSortOrder);
                }}
                className="px-3 py-1 text-sm border border-gray-300 rounded-lg"
              >
                <option value="coverage-asc">Coverage (Low to High)</option>
                <option value="coverage-desc">Coverage (High to Low)</option>
                <option value="name-asc">Name (A-Z)</option>
                <option value="name-desc">Name (Z-A)</option>
              </select>
            </div>
          }
        />
        <CardBody className="p-0">
          <div className="divide-y divide-gray-200">
            {sortedFiles.map((file) => {
              const status = getCoverageStatus(file.line_coverage);
              const StatusIcon = status.icon;

              return (
                <div
                  key={file.file_path}
                  className="px-6 py-4 hover:bg-gray-50"
                >
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center">
                      <DocumentTextIcon className="w-5 h-5 text-gray-400 mr-2" />
                      <span className="text-sm font-medium text-gray-900">
                        {formatFilePath(file.file_path, 60)}
                      </span>
                    </div>
                    <div className="flex items-center gap-4">
                      <div className="flex items-center">
                        <StatusIcon className={`w-5 h-5 mr-1 ${status.color}`} />
                        <span className={`text-sm font-medium ${getCoverageColor(file.line_coverage)}`}>
                          {file.line_coverage.toFixed(1)}%
                        </span>
                      </div>
                      <span className="text-xs text-gray-500">
                        {file.covered_lines}/{file.total_lines} lines
                      </span>
                    </div>
                  </div>
                  <ProgressBar
                    value={file.line_coverage}
                    showValue={false}
                    size="sm"
                  />
                  {file.uncovered_lines.length > 0 && file.uncovered_lines.length <= 10 && (
                    <p className="mt-2 text-xs text-gray-500">
                      Uncovered lines: {file.uncovered_lines.join(', ')}
                    </p>
                  )}
                  {file.uncovered_lines.length > 10 && (
                    <p className="mt-2 text-xs text-gray-500">
                      {file.uncovered_lines.length} uncovered lines
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        </CardBody>
      </Card>
    </div>
  );
};

export default Coverage;
