import React from 'react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts';
import { TrendSnapshot, QualityGateStatus } from '../../types';

interface TrendLineChartProps {
  data: TrendSnapshot[];
  metrics?: ('total_issues' | 'vulnerabilities' | 'bugs' | 'code_smells' | 'coverage')[];
  height?: number;
  showQualityGate?: boolean;
}

const METRIC_COLORS: Record<string, string> = {
  total_issues: '#6366f1',
  vulnerabilities: '#dc2626',
  bugs: '#ea580c',
  code_smells: '#f59e0b',
  coverage: '#10b981',
};

const METRIC_LABELS: Record<string, string> = {
  total_issues: 'Total Issues',
  vulnerabilities: 'Vulnerabilities',
  bugs: 'Bugs',
  code_smells: 'Code Smells',
  coverage: 'Coverage %',
};

const formatDate = (timestamp: string): string => {
  const date = new Date(timestamp);
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
};

const CustomTooltip = ({ active, payload, label }: any) => {
  if (!active || !payload) return null;

  const gateStatus = payload[0]?.payload?.quality_gate_status;

  return (
    <div className="bg-white p-3 border border-gray-200 rounded-lg shadow-lg">
      <p className="text-sm font-medium text-gray-900 mb-2">{formatDate(label)}</p>
      {payload.map((entry: any, index: number) => (
        <div key={index} className="flex items-center gap-2 text-sm">
          <div
            className="w-3 h-3 rounded-full"
            style={{ backgroundColor: entry.color }}
          />
          <span className="text-gray-600">{entry.name}:</span>
          <span className="font-medium">
            {entry.name === 'Coverage %' ? `${entry.value}%` : entry.value}
          </span>
        </div>
      ))}
      {gateStatus && (
        <div className="mt-2 pt-2 border-t border-gray-100">
          <span
            className={`text-xs font-medium px-2 py-1 rounded ${
              gateStatus === 'PASSED'
                ? 'bg-green-100 text-green-800'
                : gateStatus === 'FAILED'
                ? 'bg-red-100 text-red-800'
                : 'bg-yellow-100 text-yellow-800'
            }`}
          >
            {gateStatus}
          </span>
        </div>
      )}
    </div>
  );
};

const TrendLineChart: React.FC<TrendLineChartProps> = ({
  data,
  metrics = ['total_issues', 'vulnerabilities', 'bugs'],
  height = 400,
  showQualityGate = true,
}) => {
  const formattedData = React.useMemo(() => {
    return data.map((snapshot) => ({
      ...snapshot,
      date: snapshot.timestamp,
      formattedDate: formatDate(snapshot.timestamp),
    }));
  }, [data]);

  // Find quality gate change points
  const gateChangePoints = React.useMemo(() => {
    const points: { timestamp: string; status: QualityGateStatus }[] = [];
    let prevStatus: QualityGateStatus | undefined;

    data.forEach((snapshot) => {
      if (snapshot.quality_gate_status && snapshot.quality_gate_status !== prevStatus) {
        points.push({
          timestamp: snapshot.timestamp,
          status: snapshot.quality_gate_status,
        });
        prevStatus = snapshot.quality_gate_status;
      }
    });

    return points;
  }, [data]);

  if (data.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 bg-gray-50 rounded-lg">
        <p className="text-gray-500">No trend data available</p>
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={formattedData} margin={{ top: 20, right: 30, left: 20, bottom: 20 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis
          dataKey="timestamp"
          tickFormatter={formatDate}
          stroke="#6b7280"
          fontSize={12}
          tickLine={false}
        />
        <YAxis stroke="#6b7280" fontSize={12} tickLine={false} />
        <Tooltip content={<CustomTooltip />} />
        <Legend
          wrapperStyle={{ paddingTop: '20px' }}
          formatter={(value) => <span className="text-sm text-gray-600">{value}</span>}
        />

        {/* Quality gate change reference lines */}
        {showQualityGate &&
          gateChangePoints.map((point, index) => (
            <ReferenceLine
              key={index}
              x={point.timestamp}
              stroke={point.status === 'PASSED' ? '#10b981' : '#dc2626'}
              strokeDasharray="5 5"
              strokeWidth={2}
            />
          ))}

        {/* Metric lines */}
        {metrics.map((metric) => (
          <Line
            key={metric}
            type="monotone"
            dataKey={metric}
            name={METRIC_LABELS[metric]}
            stroke={METRIC_COLORS[metric]}
            strokeWidth={2}
            dot={{ r: 4, fill: METRIC_COLORS[metric] }}
            activeDot={{ r: 6 }}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
};

export default TrendLineChart;
