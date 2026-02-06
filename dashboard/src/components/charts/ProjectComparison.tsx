import React from 'react';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  Radar,
} from 'recharts';
import { ProjectSummary, ProjectComparison, TeamSummary, QualityGrade } from '../../types';
import { TrophyIcon, ArrowTrendingUpIcon, ArrowTrendingDownIcon } from '@heroicons/react/24/solid';

interface ProjectComparisonChartProps {
  projects: ProjectSummary[];
  comparisonData?: ProjectComparison;
  view?: 'bar' | 'radar' | 'table';
  metrics?: string[];
}

interface TeamComparisonProps {
  teams: TeamSummary[];
}

const QUALITY_GRADE_VALUES: Record<QualityGrade, number> = {
  A: 5,
  B: 4,
  C: 3,
  D: 2,
  E: 1,
};

const METRIC_COLORS: string[] = [
  '#6366f1',
  '#10b981',
  '#f59e0b',
  '#ef4444',
  '#8b5cf6',
  '#06b6d4',
];

const GRADE_COLORS: Record<QualityGrade, string> = {
  A: 'bg-green-500',
  B: 'bg-lime-500',
  C: 'bg-yellow-500',
  D: 'bg-orange-500',
  E: 'bg-red-500',
};

const formatMetricValue = (value: number, metric: string): string => {
  if (metric.includes('coverage') || metric.includes('duplication')) {
    return `${value.toFixed(1)}%`;
  }
  return value.toLocaleString();
};

const CustomTooltip = ({ active, payload, label }: any) => {
  if (!active || !payload) return null;

  return (
    <div className="bg-white p-3 border border-gray-200 rounded-lg shadow-lg">
      <p className="text-sm font-medium text-gray-900 mb-2">{label}</p>
      {payload.map((entry: any, index: number) => (
        <div key={index} className="flex items-center gap-2 text-sm">
          <div
            className="w-3 h-3 rounded-full"
            style={{ backgroundColor: entry.color }}
          />
          <span className="text-gray-600">{entry.name}:</span>
          <span className="font-medium">{entry.value}</span>
        </div>
      ))}
    </div>
  );
};

const ProjectComparisonChart: React.FC<ProjectComparisonChartProps> = ({
  projects,
  comparisonData,
  view = 'bar',
  metrics = ['vulnerabilities', 'bugs', 'code_smells'],
}) => {
  const [currentView, setCurrentView] = React.useState(view);
  const [selectedMetrics, setSelectedMetrics] = React.useState(metrics);

  // Prepare data for bar chart
  const barChartData = React.useMemo(() => {
    return projects.map((project) => ({
      name: project.project_name,
      vulnerabilities: project.vulnerabilities,
      bugs: project.bugs,
      code_smells: project.code_smells,
      coverage: project.coverage || 0,
      duplications: project.duplications || 0,
      lines_of_code: project.lines_of_code,
    }));
  }, [projects]);

  // Prepare data for radar chart (normalized 0-100)
  const radarChartData = React.useMemo(() => {
    const maxValues: Record<string, number> = {};

    // Find max values for normalization
    projects.forEach((project) => {
      maxValues.vulnerabilities = Math.max(maxValues.vulnerabilities || 0, project.vulnerabilities);
      maxValues.bugs = Math.max(maxValues.bugs || 0, project.bugs);
      maxValues.code_smells = Math.max(maxValues.code_smells || 0, project.code_smells);
      maxValues.coverage = 100;
      maxValues.duplications = 100;
      maxValues.maintainability = 5;
      maxValues.reliability = 5;
      maxValues.security = 5;
    });

    const metrics = [
      'Vulnerabilities',
      'Bugs',
      'Code Smells',
      'Coverage',
      'Maintainability',
      'Security',
    ];

    return metrics.map((metric) => {
      const result: Record<string, any> = { metric };

      projects.forEach((project) => {
        let value: number;
        switch (metric) {
          case 'Vulnerabilities':
            value = maxValues.vulnerabilities
              ? ((maxValues.vulnerabilities - project.vulnerabilities) / maxValues.vulnerabilities) * 100
              : 100;
            break;
          case 'Bugs':
            value = maxValues.bugs
              ? ((maxValues.bugs - project.bugs) / maxValues.bugs) * 100
              : 100;
            break;
          case 'Code Smells':
            value = maxValues.code_smells
              ? ((maxValues.code_smells - project.code_smells) / maxValues.code_smells) * 100
              : 100;
            break;
          case 'Coverage':
            value = project.coverage || 0;
            break;
          case 'Maintainability':
            value = QUALITY_GRADE_VALUES[project.maintainability_rating] * 20;
            break;
          case 'Security':
            value = QUALITY_GRADE_VALUES[project.security_rating] * 20;
            break;
          default:
            value = 50;
        }
        result[project.project_name] = value;
      });

      return result;
    });
  }, [projects]);

  // Find best and worst performers
  const rankings = React.useMemo(() => {
    if (!comparisonData?.metrics_comparison) {
      return { best: projects[0]?.project_name, worst: projects[projects.length - 1]?.project_name };
    }

    const scores: Record<string, number> = {};
    projects.forEach((p) => {
      scores[p.project_name] = 0;
    });

    comparisonData.metrics_comparison.forEach((comparison) => {
      if (comparison.best_project) {
        scores[comparison.best_project] = (scores[comparison.best_project] || 0) + 1;
      }
    });

    const sorted = Object.entries(scores).sort((a, b) => b[1] - a[1]);
    return {
      best: sorted[0]?.[0],
      worst: sorted[sorted.length - 1]?.[0],
    };
  }, [projects, comparisonData]);

  if (projects.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 bg-gray-50 rounded-lg">
        <p className="text-gray-500">Select projects to compare</p>
      </div>
    );
  }

  return (
    <div>
      {/* View Toggle and Highlights */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex gap-2">
          <button
            onClick={() => setCurrentView('bar')}
            className={`px-3 py-1.5 text-sm font-medium rounded-lg transition-colors ${
              currentView === 'bar'
                ? 'bg-primary-100 text-primary-700'
                : 'text-gray-600 hover:bg-gray-100'
            }`}
          >
            Bar Chart
          </button>
          <button
            onClick={() => setCurrentView('radar')}
            className={`px-3 py-1.5 text-sm font-medium rounded-lg transition-colors ${
              currentView === 'radar'
                ? 'bg-primary-100 text-primary-700'
                : 'text-gray-600 hover:bg-gray-100'
            }`}
          >
            Radar Chart
          </button>
          <button
            onClick={() => setCurrentView('table')}
            className={`px-3 py-1.5 text-sm font-medium rounded-lg transition-colors ${
              currentView === 'table'
                ? 'bg-primary-100 text-primary-700'
                : 'text-gray-600 hover:bg-gray-100'
            }`}
          >
            Table
          </button>
        </div>

        {/* Best/Worst Indicators */}
        <div className="flex items-center gap-4">
          {rankings.best && (
            <div className="flex items-center gap-2 text-sm">
              <TrophyIcon className="w-5 h-5 text-yellow-500" />
              <span className="text-gray-600">Best:</span>
              <span className="font-medium text-green-600">{rankings.best}</span>
            </div>
          )}
        </div>
      </div>

      {/* Bar Chart View */}
      {currentView === 'bar' && (
        <ResponsiveContainer width="100%" height={400}>
          <BarChart data={barChartData} margin={{ top: 20, right: 30, left: 20, bottom: 60 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
            <XAxis
              dataKey="name"
              angle={-45}
              textAnchor="end"
              height={80}
              stroke="#6b7280"
              fontSize={12}
            />
            <YAxis stroke="#6b7280" fontSize={12} />
            <Tooltip content={<CustomTooltip />} />
            <Legend wrapperStyle={{ paddingTop: '20px' }} />
            {selectedMetrics.includes('vulnerabilities') && (
              <Bar dataKey="vulnerabilities" name="Vulnerabilities" fill="#ef4444" />
            )}
            {selectedMetrics.includes('bugs') && (
              <Bar dataKey="bugs" name="Bugs" fill="#f59e0b" />
            )}
            {selectedMetrics.includes('code_smells') && (
              <Bar dataKey="code_smells" name="Code Smells" fill="#6366f1" />
            )}
          </BarChart>
        </ResponsiveContainer>
      )}

      {/* Radar Chart View */}
      {currentView === 'radar' && (
        <ResponsiveContainer width="100%" height={400}>
          <RadarChart data={radarChartData}>
            <PolarGrid stroke="#e5e7eb" />
            <PolarAngleAxis dataKey="metric" tick={{ fill: '#6b7280', fontSize: 12 }} />
            <PolarRadiusAxis angle={30} domain={[0, 100]} tick={{ fill: '#9ca3af', fontSize: 10 }} />
            {projects.map((project, index) => (
              <Radar
                key={project.project_name}
                name={project.project_name}
                dataKey={project.project_name}
                stroke={METRIC_COLORS[index % METRIC_COLORS.length]}
                fill={METRIC_COLORS[index % METRIC_COLORS.length]}
                fillOpacity={0.2}
                strokeWidth={2}
              />
            ))}
            <Legend />
            <Tooltip />
          </RadarChart>
        </ResponsiveContainer>
      )}

      {/* Table View */}
      {currentView === 'table' && (
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Project
                </th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Quality Gate
                </th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Vulnerabilities
                </th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Bugs
                </th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Code Smells
                </th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Coverage
                </th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Duplications
                </th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Security
                </th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Reliability
                </th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Maintainability
                </th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
              {projects.map((project) => (
                <tr key={project.project_name} className="hover:bg-gray-50">
                  <td className="px-4 py-4 whitespace-nowrap">
                    <div className="flex items-center">
                      <span className="text-sm font-medium text-gray-900">
                        {project.project_name}
                      </span>
                      {project.project_name === rankings.best && (
                        <TrophyIcon className="w-4 h-4 ml-2 text-yellow-500" />
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-4 whitespace-nowrap text-center">
                    <span
                      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                        project.quality_gate_status === 'PASSED'
                          ? 'bg-green-100 text-green-800'
                          : project.quality_gate_status === 'FAILED'
                          ? 'bg-red-100 text-red-800'
                          : 'bg-yellow-100 text-yellow-800'
                      }`}
                    >
                      {project.quality_gate_status}
                    </span>
                  </td>
                  <td className="px-4 py-4 whitespace-nowrap text-center text-sm text-gray-900">
                    {project.vulnerabilities}
                  </td>
                  <td className="px-4 py-4 whitespace-nowrap text-center text-sm text-gray-900">
                    {project.bugs}
                  </td>
                  <td className="px-4 py-4 whitespace-nowrap text-center text-sm text-gray-900">
                    {project.code_smells}
                  </td>
                  <td className="px-4 py-4 whitespace-nowrap text-center text-sm text-gray-900">
                    {project.coverage ? `${project.coverage.toFixed(1)}%` : '-'}
                  </td>
                  <td className="px-4 py-4 whitespace-nowrap text-center text-sm text-gray-900">
                    {project.duplications ? `${project.duplications.toFixed(1)}%` : '-'}
                  </td>
                  <td className="px-4 py-4 whitespace-nowrap text-center">
                    <span
                      className={`inline-flex items-center justify-center w-6 h-6 rounded-full text-xs font-bold text-white ${
                        GRADE_COLORS[project.security_rating]
                      }`}
                    >
                      {project.security_rating}
                    </span>
                  </td>
                  <td className="px-4 py-4 whitespace-nowrap text-center">
                    <span
                      className={`inline-flex items-center justify-center w-6 h-6 rounded-full text-xs font-bold text-white ${
                        GRADE_COLORS[project.reliability_rating]
                      }`}
                    >
                      {project.reliability_rating}
                    </span>
                  </td>
                  <td className="px-4 py-4 whitespace-nowrap text-center">
                    <span
                      className={`inline-flex items-center justify-center w-6 h-6 rounded-full text-xs font-bold text-white ${
                        GRADE_COLORS[project.maintainability_rating]
                      }`}
                    >
                      {project.maintainability_rating}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

const TeamComparison: React.FC<TeamComparisonProps> = ({ teams }) => {
  if (teams.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 bg-gray-50 rounded-lg">
        <p className="text-gray-500">No teams to compare</p>
      </div>
    );
  }

  const teamBarData = teams.map((team) => ({
    name: team.team_name,
    issues: team.aggregate_metrics.total_issues,
    vulnerabilities: team.aggregate_metrics.total_vulnerabilities,
    coverage: team.aggregate_metrics.average_coverage,
    projects: team.projects.length,
  }));

  return (
    <div>
      <h3 className="text-lg font-medium text-gray-900 mb-4">Team Comparison</h3>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4 mb-6">
        {teams.map((team) => (
          <div key={team.team_id} className="bg-white p-4 rounded-lg border border-gray-200">
            <h4 className="font-medium text-gray-900">{team.team_name}</h4>
            <p className="text-sm text-gray-500 mb-3">{team.projects.length} projects</p>

            <div className="space-y-2 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-500">Total Issues:</span>
                <span className="font-medium">{team.aggregate_metrics.total_issues}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Vulnerabilities:</span>
                <span className="font-medium text-red-600">
                  {team.aggregate_metrics.total_vulnerabilities}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Avg Coverage:</span>
                <span className="font-medium">
                  {team.aggregate_metrics.average_coverage.toFixed(1)}%
                </span>
              </div>
              <div className="flex justify-between items-center">
                <span className="text-gray-500">Avg Rating:</span>
                <span
                  className={`inline-flex items-center justify-center w-6 h-6 rounded-full text-xs font-bold text-white ${
                    GRADE_COLORS[team.aggregate_metrics.average_quality_rating]
                  }`}
                >
                  {team.aggregate_metrics.average_quality_rating}
                </span>
              </div>
            </div>
          </div>
        ))}
      </div>

      <ResponsiveContainer width="100%" height={300}>
        <BarChart data={teamBarData} margin={{ top: 20, right: 30, left: 20, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
          <XAxis dataKey="name" stroke="#6b7280" fontSize={12} />
          <YAxis stroke="#6b7280" fontSize={12} />
          <Tooltip content={<CustomTooltip />} />
          <Legend />
          <Bar dataKey="issues" name="Total Issues" fill="#6366f1" />
          <Bar dataKey="vulnerabilities" name="Vulnerabilities" fill="#ef4444" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
};

export { ProjectComparisonChart, TeamComparison };
export default ProjectComparisonChart;
