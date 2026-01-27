import React from 'react';
import { Severity, QualityGrade, QualityGateStatus, IssueType } from '../../types';

interface SeverityBadgeProps {
  severity: Severity;
  className?: string;
}

interface QualityBadgeProps {
  grade: QualityGrade;
  className?: string;
}

interface StatusBadgeProps {
  status: QualityGateStatus;
  className?: string;
}

interface IssueTypeBadgeProps {
  type: IssueType;
  className?: string;
}

export const SeverityBadge: React.FC<SeverityBadgeProps> = ({ severity, className = '' }) => {
  const colors: Record<Severity, string> = {
    BLOCKER: 'badge-blocker',
    CRITICAL: 'badge-critical',
    MAJOR: 'badge-major',
    MINOR: 'badge-minor',
    INFO: 'badge-info',
  };

  return (
    <span className={`badge ${colors[severity]} ${className}`}>
      {severity}
    </span>
  );
};

export const QualityBadge: React.FC<QualityBadgeProps> = ({ grade, className = '' }) => {
  return (
    <span className={`quality-badge quality-${grade} ${className}`}>
      {grade}
    </span>
  );
};

export const StatusBadge: React.FC<StatusBadgeProps> = ({ status, className = '' }) => {
  const colors: Record<QualityGateStatus, string> = {
    PASSED: 'bg-green-100 text-green-800',
    FAILED: 'bg-red-100 text-red-800',
    WARNING: 'bg-amber-100 text-amber-800',
  };

  const labels: Record<QualityGateStatus, string> = {
    PASSED: 'Passed',
    FAILED: 'Failed',
    WARNING: 'Warning',
  };

  return (
    <span className={`badge ${colors[status]} ${className}`}>
      {labels[status]}
    </span>
  );
};

export const IssueTypeBadge: React.FC<IssueTypeBadgeProps> = ({ type, className = '' }) => {
  const colors: Record<IssueType, string> = {
    BUG: 'bg-red-100 text-red-800',
    VULNERABILITY: 'bg-orange-100 text-orange-800',
    CODE_SMELL: 'bg-yellow-100 text-yellow-800',
    SECURITY_HOTSPOT: 'bg-purple-100 text-purple-800',
    DUPLICATION: 'bg-blue-100 text-blue-800',
  };

  const labels: Record<IssueType, string> = {
    BUG: 'Bug',
    VULNERABILITY: 'Vulnerability',
    CODE_SMELL: 'Code Smell',
    SECURITY_HOTSPOT: 'Security Hotspot',
    DUPLICATION: 'Duplication',
  };

  return (
    <span className={`badge ${colors[type]} ${className}`}>
      {labels[type]}
    </span>
  );
};
