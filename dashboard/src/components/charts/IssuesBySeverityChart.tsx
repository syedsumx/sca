import React from 'react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell } from 'recharts';
import { Issue, Severity } from '../../types';

interface IssuesBySeverityChartProps {
  issues: Issue[];
}

const COLORS: Record<Severity, string> = {
  BLOCKER: '#dc2626',
  CRITICAL: '#ea580c',
  MAJOR: '#f59e0b',
  MINOR: '#84cc16',
  INFO: '#06b6d4',
};

const SEVERITY_ORDER: Severity[] = ['BLOCKER', 'CRITICAL', 'MAJOR', 'MINOR', 'INFO'];

const IssuesBySeverityChart: React.FC<IssuesBySeverityChartProps> = ({ issues }) => {
  const data = React.useMemo(() => {
    const counts: Record<string, number> = {};
    SEVERITY_ORDER.forEach((sev) => {
      counts[sev] = 0;
    });

    issues.forEach((issue) => {
      counts[issue.severity] = (counts[issue.severity] || 0) + 1;
    });

    return SEVERITY_ORDER.map((severity) => ({
      severity,
      count: counts[severity],
      fill: COLORS[severity],
    }));
  }, [issues]);

  return (
    <ResponsiveContainer width="100%" height={300}>
      <BarChart data={data} layout="vertical">
        <CartesianGrid strokeDasharray="3 3" horizontal={false} />
        <XAxis type="number" />
        <YAxis type="category" dataKey="severity" width={80} />
        <Tooltip
          contentStyle={{
            backgroundColor: 'white',
            border: '1px solid #e5e7eb',
            borderRadius: '0.5rem',
          }}
        />
        <Bar
          dataKey="count"
          radius={[0, 4, 4, 0]}
          label={{ position: 'right', fill: '#6b7280' }}
        >
          {data.map((entry, index) => (
            <Cell key={`cell-${index}`} fill={entry.fill} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
};

export default IssuesBySeverityChart;
