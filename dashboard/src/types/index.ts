// Severity and Issue Types
export type Severity = 'BLOCKER' | 'CRITICAL' | 'MAJOR' | 'MINOR' | 'INFO';
export type IssueType = 'BUG' | 'VULNERABILITY' | 'CODE_SMELL' | 'SECURITY_HOTSPOT' | 'DUPLICATION';
export type QualityGrade = 'A' | 'B' | 'C' | 'D' | 'E';
export type QualityGateStatus = 'PASSED' | 'FAILED' | 'WARNING';

// Issue
export interface Location {
  file_path: string;
  start_line: number;
  end_line: number;
  start_column?: number;
  end_column?: number;
}

export interface Issue {
  id: string;
  rule_id: string;
  rule_name: string;
  severity: Severity;
  issue_type: IssueType;
  message: string;
  location: Location;
  effort_minutes?: number;
  tags: string[];
  snippet?: string;
  suggestion?: string;
}

// Metrics
export interface FileMetrics {
  file_path: string;
  lines_of_code: number;
  comment_lines: number;
  blank_lines: number;
  complexity: number;
  functions: number;
  classes: number;
  issues_count: number;
}

export interface ProjectMetrics {
  total_files: number;
  total_lines: number;
  total_code_lines: number;
  total_comment_lines: number;
  total_blank_lines: number;
  average_complexity: number;
  total_functions: number;
  total_classes: number;
  languages: Record<string, number>;
}

// Analysis Result
export interface AnalysisResult {
  project_name: string;
  analysis_id: string;
  timestamp: string;
  duration_seconds: number;
  status: 'SUCCESS' | 'FAILURE' | 'PARTIAL';
  issues: Issue[];
  metrics: ProjectMetrics;
  file_metrics: FileMetrics[];
  quality_gate: QualityGateResult;
}

// Quality Gate
export interface QualityGateCondition {
  metric: string;
  operator: string;
  threshold: number;
  actual_value: number;
  status: QualityGateStatus;
}

export interface QualityGateResult {
  status: QualityGateStatus;
  conditions: QualityGateCondition[];
}

// Duplication
export interface DuplicateBlock {
  file_path: string;
  start_line: number;
  end_line: number;
  lines: number;
  fingerprint: string;
}

export interface DuplicationGroup {
  id: string;
  fingerprint: string;
  token_count: number;
  line_count: number;
  blocks: DuplicateBlock[];
}

export interface DuplicationResult {
  total_duplicated_lines: number;
  total_duplicated_blocks: number;
  duplication_percentage: number;
  groups: DuplicationGroup[];
}

// Dependencies
export interface Dependency {
  name: string;
  version: string;
  ecosystem: string;
  source_file: string;
  is_dev: boolean;
}

export interface Vulnerability {
  id: string;
  severity: Severity;
  summary: string;
  details?: string;
  affected_package: string;
  affected_versions: string;
  fixed_version?: string;
  references: string[];
  cvss_score?: number;
}

export interface DependencyScanResult {
  total_dependencies: number;
  direct_dependencies: number;
  dev_dependencies: number;
  vulnerable_count: number;
  dependencies: Dependency[];
  vulnerabilities: Vulnerability[];
}

// Coverage
export interface FileCoverage {
  file_path: string;
  line_coverage: number;
  branch_coverage?: number;
  covered_lines: number;
  total_lines: number;
  uncovered_lines: number[];
}

export interface CoverageResult {
  line_coverage: number;
  branch_coverage?: number;
  total_lines: number;
  covered_lines: number;
  uncovered_lines: number;
  files: FileCoverage[];
}

// Git
export interface CommitInfo {
  hash: string;
  short_hash: string;
  author_name: string;
  author_email: string;
  date: string;
  message: string;
}

export interface BlameInfo {
  line_number: number;
  commit_hash: string;
  author_name: string;
  author_email: string;
  date: string;
  content: string;
}

export interface ChangedFile {
  file_path: string;
  status: string;
  insertions: number;
  deletions: number;
  added_lines: number[];
}

// Project Summary
export interface ProjectSummary {
  project_name: string;
  last_analysis?: string;
  quality_gate_status?: QualityGateStatus;
  bugs: number;
  vulnerabilities: number;
  code_smells: number;
  coverage?: number;
  duplications?: number;
  lines_of_code: number;
  reliability_rating: QualityGrade;
  security_rating: QualityGrade;
  maintainability_rating: QualityGrade;
}

// Dashboard Stats
export interface DashboardStats {
  total_projects: number;
  total_issues: number;
  total_vulnerabilities: number;
  projects_passing: number;
  projects_failing: number;
  recent_analyses: AnalysisSummary[];
}

export interface AnalysisSummary {
  project_name: string;
  analysis_id: string;
  timestamp: string;
  issues_count: number;
  quality_gate_status: QualityGateStatus;
}

// Trend Data
export interface TrendSnapshot {
  timestamp: string;
  total_issues: number;
  vulnerabilities: number;
  bugs: number;
  code_smells: number;
  coverage?: number;
  duplication_pct?: number;
  quality_gate_status?: QualityGateStatus;
}

export interface TrendData {
  project_name: string;
  snapshots: TrendSnapshot[];
  delta?: TrendDelta;
}

export interface TrendDelta {
  total_issues: { current: number; previous: number; delta: number };
  vulnerabilities: { current: number; previous: number; delta: number };
  bugs: { current: number; previous: number; delta: number };
  code_smells: { current: number; previous: number; delta: number };
}

// Hotspot Data
export interface FileHotspot {
  file_path: string;
  issues_count: number;
  complexity: number;
  churn: number;
  last_modified: string;
  risk_score: number;
  authors: string[];
}

export interface HotspotData {
  files: FileHotspot[];
  directories: DirectoryHotspot[];
}

export interface DirectoryHotspot {
  path: string;
  total_issues: number;
  total_files: number;
  average_complexity: number;
  risk_score: number;
}

// Timeline Event
export interface TimelineEvent {
  id: string;
  timestamp: string;
  event_type: 'issue_created' | 'issue_fixed' | 'scan_complete' | 'quality_gate_change' | 'vulnerability_found';
  severity?: Severity;
  title: string;
  description: string;
  file_path?: string;
  rule_id?: string;
  project_name: string;
}

// Team/Project Comparison
export interface TeamSummary {
  team_id: string;
  team_name: string;
  projects: ProjectSummary[];
  aggregate_metrics: {
    total_issues: number;
    total_vulnerabilities: number;
    average_coverage: number;
    average_duplication: number;
    average_quality_rating: QualityGrade;
  };
}

export interface ProjectComparison {
  projects: ProjectSummary[];
  metrics_comparison: {
    metric_name: string;
    values: { project_name: string; value: number }[];
    best_project: string;
    worst_project: string;
  }[];
}
