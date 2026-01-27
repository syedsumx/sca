import React from 'react';
import { PieChart, Pie, Cell, ResponsiveContainer, Legend, Tooltip } from 'recharts';
import { Issue, IssueType } from '../../types';

interface IssuesByTypeChartProps {
  issues: Issue[];
}

const COLORS: Record<IssueType, string> = {
  BUG: '#ef4444',
  VULNERABILITY: '#f97316',
  CODE_SMELL: '#eab308',
  SECURITY_HOTSPOT: '#a855f7',
  DUPLICATION: '#3b82f6',
};

const LABELS: Record<IssueType, string> = {
  BUG: 'Bugs',
  VULNERABILITY: 'Vulnerabilities',
  CODE_SMELL: 'Code Smells',
  SECURITY_HOTSPOT: 'Security Hotspots',
  DUPLICATION: 'Duplications',
};

const IssuesByTypeChart: React.FC<IssuesByTypeChartProps> = ({ issues }) => {
  const data = React.useMemo(() => {
    const counts: Record<string, number> = {};
    issues.forEach((issue) => {
      counts[issue.issue_type] = (counts[issue.issue_type] || 0) + 1;
    });

    return Object.entries(counts).map(([type, count]) => ({
      name: LABELS[type as IssueType] || type,
      value: count,
      color: COLORS[type as IssueType] || '#6b7280',
    }));
  }, [issues]);

  if (data.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-500">
        No issues found
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={300}>
      <PieChart>
        <Pie
          data={data}
          cx="50%"
          cy="50%"
          labelLine={false}
          outerRadius={100}
          fill="#8884d8"
          dataKey="value"
          label={({ name, percent }) => `${name} (${(percent * 100).toFixed(0)}%)`}
        >
          {data.map((entry, index) => (
            <Cell key={`cell-${index}`} fill={entry.color} />
          ))}
        </Pie>
        <Tooltip />
        <Legend />
      </PieChart>
    </ResponsiveContainer>
  );
};

export default IssuesByTypeChart;
