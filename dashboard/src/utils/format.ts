import { Severity, QualityGrade, QualityGateStatus } from '../types';

/**
 * Format a number with thousands separators
 */
export function formatNumber(num: number): string {
  return new Intl.NumberFormat().format(num);
}

/**
 * Format a percentage value
 */
export function formatPercentage(value: number, decimals = 1): string {
  return `${value.toFixed(decimals)}%`;
}

/**
 * Format duration in seconds to human-readable string
 */
export function formatDuration(seconds: number): string {
  if (seconds < 60) {
    return `${seconds.toFixed(1)}s`;
  }
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = Math.floor(seconds % 60);
  if (minutes < 60) {
    return `${minutes}m ${remainingSeconds}s`;
  }
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return `${hours}h ${remainingMinutes}m`;
}

/**
 * Format timestamp to localized date string
 */
export function formatDate(timestamp: string): string {
  return new Date(timestamp).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}

/**
 * Format timestamp to relative time string
 */
export function formatRelativeTime(timestamp: string): string {
  const date = new Date(timestamp);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSeconds = Math.floor(diffMs / 1000);
  const diffMinutes = Math.floor(diffSeconds / 60);
  const diffHours = Math.floor(diffMinutes / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffSeconds < 60) {
    return 'just now';
  }
  if (diffMinutes < 60) {
    return `${diffMinutes} minute${diffMinutes === 1 ? '' : 's'} ago`;
  }
  if (diffHours < 24) {
    return `${diffHours} hour${diffHours === 1 ? '' : 's'} ago`;
  }
  if (diffDays < 7) {
    return `${diffDays} day${diffDays === 1 ? '' : 's'} ago`;
  }
  return formatDate(timestamp);
}

/**
 * Format file path for display (truncate long paths)
 */
export function formatFilePath(path: string, maxLength = 50): string {
  if (path.length <= maxLength) {
    return path;
  }
  const parts = path.split('/');
  if (parts.length <= 2) {
    return `...${path.slice(-maxLength + 3)}`;
  }
  const filename = parts.pop()!;
  const parent = parts.pop()!;
  return `.../${parent}/${filename}`;
}

/**
 * Get severity color class
 */
export function getSeverityColor(severity: Severity): string {
  const colors: Record<Severity, string> = {
    BLOCKER: 'text-red-600',
    CRITICAL: 'text-orange-600',
    MAJOR: 'text-amber-600',
    MINOR: 'text-lime-600',
    INFO: 'text-cyan-600',
  };
  return colors[severity] || 'text-gray-600';
}

/**
 * Get severity background color class
 */
export function getSeverityBgColor(severity: Severity): string {
  const colors: Record<Severity, string> = {
    BLOCKER: 'bg-red-100',
    CRITICAL: 'bg-orange-100',
    MAJOR: 'bg-amber-100',
    MINOR: 'bg-lime-100',
    INFO: 'bg-cyan-100',
  };
  return colors[severity] || 'bg-gray-100';
}

/**
 * Get quality gate status color class
 */
export function getQualityGateColor(status: QualityGateStatus): string {
  const colors: Record<QualityGateStatus, string> = {
    PASSED: 'text-green-600',
    FAILED: 'text-red-600',
    WARNING: 'text-amber-600',
  };
  return colors[status] || 'text-gray-600';
}

/**
 * Get quality grade color class
 */
export function getGradeColor(grade: QualityGrade): string {
  const colors: Record<QualityGrade, string> = {
    A: 'bg-green-500',
    B: 'bg-lime-500',
    C: 'bg-amber-500',
    D: 'bg-orange-500',
    E: 'bg-red-500',
  };
  return colors[grade] || 'bg-gray-500';
}

/**
 * Calculate quality grade from a value
 */
export function calculateGrade(
  value: number,
  thresholds = { A: 90, B: 80, C: 70, D: 60 }
): QualityGrade {
  if (value >= thresholds.A) return 'A';
  if (value >= thresholds.B) return 'B';
  if (value >= thresholds.C) return 'C';
  if (value >= thresholds.D) return 'D';
  return 'E';
}

/**
 * Calculate technical debt in human readable format
 */
export function formatTechnicalDebt(minutes: number): string {
  if (minutes < 60) {
    return `${minutes}min`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 8) {
    const remainingMinutes = minutes % 60;
    return remainingMinutes > 0 ? `${hours}h ${remainingMinutes}min` : `${hours}h`;
  }
  const days = Math.floor(hours / 8);
  const remainingHours = hours % 8;
  return remainingHours > 0 ? `${days}d ${remainingHours}h` : `${days}d`;
}

/**
 * Truncate text with ellipsis
 */
export function truncate(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength - 3)}...`;
}
