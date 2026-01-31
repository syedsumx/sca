import React from 'react';
import { QualityBadge } from './Badge';
import { QualityGrade } from '../../types';

interface MetricCardProps {
  title: string;
  value: string | number;
  subtitle?: string;
  icon?: React.ReactNode;
  grade?: QualityGrade;
  trend?: {
    direction: 'up' | 'down' | 'neutral';
    value: string;
    positive?: boolean;
  };
  className?: string;
}

const MetricCard: React.FC<MetricCardProps> = ({
  title,
  value,
  subtitle,
  icon,
  grade,
  trend,
  className = '',
}) => {
  const getTrendColor = () => {
    if (!trend) return '';
    if (trend.direction === 'neutral') return 'text-gray-500';
    if (trend.positive !== undefined) {
      return trend.positive ? 'text-green-600' : 'text-red-600';
    }
    return trend.direction === 'up' ? 'text-green-600' : 'text-red-600';
  };

  const getTrendIcon = () => {
    if (!trend) return null;
    if (trend.direction === 'up') {
      return (
        <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
          <path fillRule="evenodd" d="M5.293 9.707a1 1 0 010-1.414l4-4a1 1 0 011.414 0l4 4a1 1 0 01-1.414 1.414L11 7.414V15a1 1 0 11-2 0V7.414L6.707 9.707a1 1 0 01-1.414 0z" clipRule="evenodd" />
        </svg>
      );
    }
    if (trend.direction === 'down') {
      return (
        <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
          <path fillRule="evenodd" d="M14.707 10.293a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 111.414-1.414L9 12.586V5a1 1 0 012 0v7.586l2.293-2.293a1 1 0 011.414 0z" clipRule="evenodd" />
        </svg>
      );
    }
    return null;
  };

  return (
    <div className={`metric-card ${className}`}>
      <div className="flex items-center justify-between">
        <div className="flex-1">
          <p className="text-sm font-medium text-gray-500">{title}</p>
          <div className="flex items-baseline mt-1">
            <span className="metric-value">{value}</span>
            {grade && (
              <span className="ml-2">
                <QualityBadge grade={grade} />
              </span>
            )}
          </div>
          {subtitle && (
            <p className="metric-label">{subtitle}</p>
          )}
          {trend && (
            <div className={`flex items-center mt-2 text-sm ${getTrendColor()}`}>
              {getTrendIcon()}
              <span className="ml-1">{trend.value}</span>
            </div>
          )}
        </div>
        {icon && (
          <div className="flex-shrink-0 p-3 bg-primary-100 rounded-lg text-primary-600">
            {icon}
          </div>
        )}
      </div>
    </div>
  );
};

export default MetricCard;
