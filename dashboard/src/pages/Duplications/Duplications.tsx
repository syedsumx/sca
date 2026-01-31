import React, { useState } from 'react';
import { DocumentDuplicateIcon, ChevronDownIcon } from '@heroicons/react/24/outline';
import {
  Card,
  CardHeader,
  CardBody,
  MetricCard,
  ProgressBar,
} from '../../components/common';
import { DuplicationGroup, DuplicateBlock } from '../../types';
import { formatFilePath, formatNumber } from '../../utils/format';

// Mock data
const mockDuplicationResult = {
  total_duplicated_lines: 456,
  total_duplicated_blocks: 23,
  duplication_percentage: 4.8,
  groups: [
    {
      id: 'dup-1',
      fingerprint: 'abc123',
      token_count: 150,
      line_count: 25,
      blocks: [
        { file_path: 'src/handlers/user_handler.py', start_line: 45, end_line: 70, lines: 25, fingerprint: 'abc123' },
        { file_path: 'src/handlers/admin_handler.py', start_line: 32, end_line: 57, lines: 25, fingerprint: 'abc123' },
        { file_path: 'src/handlers/guest_handler.py', start_line: 18, end_line: 43, lines: 25, fingerprint: 'abc123' },
      ],
    },
    {
      id: 'dup-2',
      fingerprint: 'def456',
      token_count: 95,
      line_count: 18,
      blocks: [
        { file_path: 'src/services/auth.py', start_line: 120, end_line: 138, lines: 18, fingerprint: 'def456' },
        { file_path: 'src/services/oauth.py', start_line: 85, end_line: 103, lines: 18, fingerprint: 'def456' },
      ],
    },
    {
      id: 'dup-3',
      fingerprint: 'ghi789',
      token_count: 78,
      line_count: 12,
      blocks: [
        { file_path: 'src/utils/validators.py', start_line: 23, end_line: 35, lines: 12, fingerprint: 'ghi789' },
        { file_path: 'src/api/validators.py', start_line: 45, end_line: 57, lines: 12, fingerprint: 'ghi789' },
      ],
    },
    {
      id: 'dup-4',
      fingerprint: 'jkl012',
      token_count: 120,
      line_count: 20,
      blocks: [
        { file_path: 'src/models/user.py', start_line: 15, end_line: 35, lines: 20, fingerprint: 'jkl012' },
        { file_path: 'src/models/account.py', start_line: 22, end_line: 42, lines: 20, fingerprint: 'jkl012' },
        { file_path: 'src/models/profile.py', start_line: 10, end_line: 30, lines: 20, fingerprint: 'jkl012' },
      ],
    },
  ] as DuplicationGroup[],
};

const Duplications: React.FC = () => {
  const [expandedGroup, setExpandedGroup] = useState<string | null>(null);
  const data = mockDuplicationResult;

  const getDuplicationColor = (percentage: number) => {
    if (percentage < 3) return 'text-green-600';
    if (percentage < 5) return 'text-lime-600';
    if (percentage < 10) return 'text-amber-600';
    if (percentage < 20) return 'text-orange-600';
    return 'text-red-600';
  };

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Code Duplications</h1>
        <p className="mt-1 text-sm text-gray-500">
          Identify and manage duplicated code blocks across your project
        </p>
      </div>

      {/* Summary Metrics */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <MetricCard
          title="Duplication Rate"
          value={`${data.duplication_percentage.toFixed(1)}%`}
          subtitle="of total code"
          icon={<DocumentDuplicateIcon className="w-6 h-6" />}
        />
        <MetricCard
          title="Duplicated Lines"
          value={formatNumber(data.total_duplicated_lines)}
          subtitle="total duplicated lines"
        />
        <MetricCard
          title="Duplicate Blocks"
          value={data.total_duplicated_blocks}
          subtitle="detected groups"
        />
      </div>

      {/* Duplication Threshold */}
      <Card>
        <CardBody>
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-medium text-gray-700">Duplication Threshold</span>
            <span className={`text-sm font-medium ${getDuplicationColor(data.duplication_percentage)}`}>
              {data.duplication_percentage.toFixed(1)}% (Target: &lt;3%)
            </span>
          </div>
          <div className="relative">
            <ProgressBar
              value={data.duplication_percentage}
              max={20}
              showValue={false}
              color={data.duplication_percentage < 3 ? 'success' : data.duplication_percentage < 10 ? 'warning' : 'danger'}
            />
            {/* Threshold marker */}
            <div
              className="absolute top-0 bottom-0 w-0.5 bg-green-600"
              style={{ left: '15%' }}
            >
              <span className="absolute -top-5 -translate-x-1/2 text-xs text-green-600">3%</span>
            </div>
          </div>
        </CardBody>
      </Card>

      {/* Duplication Groups */}
      <Card>
        <CardHeader
          title="Duplication Groups"
          subtitle={`${data.groups.length} groups of duplicated code`}
        />
        <CardBody className="p-0">
          <div className="divide-y divide-gray-200">
            {data.groups.map((group) => (
              <div key={group.id}>
                <div
                  className="px-6 py-4 cursor-pointer hover:bg-gray-50"
                  onClick={() => setExpandedGroup(expandedGroup === group.id ? null : group.id)}
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center space-x-4">
                      <div className="p-2 bg-blue-100 rounded-lg">
                        <DocumentDuplicateIcon className="w-5 h-5 text-blue-600" />
                      </div>
                      <div>
                        <p className="text-sm font-medium text-gray-900">
                          {group.line_count} lines duplicated in {group.blocks.length} locations
                        </p>
                        <p className="text-xs text-gray-500">
                          {group.token_count} tokens • ID: {group.fingerprint.slice(0, 8)}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center space-x-4">
                      <span className="px-2 py-1 text-xs font-medium bg-blue-100 text-blue-800 rounded">
                        {group.blocks.length} files
                      </span>
                      <ChevronDownIcon
                        className={`w-5 h-5 text-gray-400 transition-transform ${
                          expandedGroup === group.id ? 'rotate-180' : ''
                        }`}
                      />
                    </div>
                  </div>
                </div>

                {/* Expanded Block Details */}
                {expandedGroup === group.id && (
                  <div className="px-6 pb-4 bg-gray-50 border-t border-gray-100">
                    <div className="pt-4 space-y-3">
                      {group.blocks.map((block, index) => (
                        <div
                          key={`${block.file_path}-${block.start_line}`}
                          className="flex items-center justify-between p-3 bg-white rounded-lg border border-gray-200"
                        >
                          <div className="flex items-center space-x-3">
                            <span className="flex items-center justify-center w-6 h-6 text-xs font-medium bg-gray-200 text-gray-700 rounded-full">
                              {index + 1}
                            </span>
                            <div>
                              <p className="text-sm font-medium text-gray-900">
                                {formatFilePath(block.file_path)}
                              </p>
                              <p className="text-xs text-gray-500">
                                Lines {block.start_line} - {block.end_line}
                              </p>
                            </div>
                          </div>
                          <button className="text-sm text-primary-600 hover:text-primary-700">
                            View code
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ))}

            {data.groups.length === 0 && (
              <div className="px-6 py-12 text-center text-gray-500">
                No code duplications detected
              </div>
            )}
          </div>
        </CardBody>
      </Card>
    </div>
  );
};

export default Duplications;
