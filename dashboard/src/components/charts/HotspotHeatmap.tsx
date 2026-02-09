import React from 'react';
import { FileHotspot, DirectoryHotspot, HotspotData } from '../../types';
import { FireIcon, FolderIcon, DocumentIcon } from '@heroicons/react/24/solid';

interface HotspotHeatmapProps {
  data: HotspotData;
  view?: 'files' | 'directories' | 'treemap';
  maxItems?: number;
  onFileClick?: (filePath: string) => void;
}

// Calculate color based on risk score (0-100)
const getRiskColor = (score: number): string => {
  if (score >= 80) return 'bg-red-500';
  if (score >= 60) return 'bg-orange-500';
  if (score >= 40) return 'bg-yellow-500';
  if (score >= 20) return 'bg-lime-500';
  return 'bg-green-500';
};

const getRiskTextColor = (score: number): string => {
  if (score >= 80) return 'text-red-700';
  if (score >= 60) return 'text-orange-700';
  if (score >= 40) return 'text-yellow-700';
  if (score >= 20) return 'text-lime-700';
  return 'text-green-700';
};

const getRiskBgColor = (score: number): string => {
  if (score >= 80) return 'bg-red-50';
  if (score >= 60) return 'bg-orange-50';
  if (score >= 40) return 'bg-yellow-50';
  if (score >= 20) return 'bg-lime-50';
  return 'bg-green-50';
};

const formatPath = (path: string): { directory: string; filename: string } => {
  const parts = path.split('/');
  const filename = parts.pop() || path;
  const directory = parts.join('/') || '.';
  return { directory, filename };
};

interface FileHotspotCardProps {
  file: FileHotspot;
  rank: number;
  onClick?: () => void;
}

const FileHotspotCard: React.FC<FileHotspotCardProps> = ({ file, rank, onClick }) => {
  const { directory, filename } = formatPath(file.file_path);

  return (
    <div
      onClick={onClick}
      className={`p-4 rounded-lg border cursor-pointer transition-all hover:shadow-md ${getRiskBgColor(
        file.risk_score
      )} border-gray-200`}
    >
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2">
          <span
            className={`flex items-center justify-center w-6 h-6 rounded-full text-xs font-bold text-white ${getRiskColor(
              file.risk_score
            )}`}
          >
            {rank}
          </span>
          <div className="min-w-0">
            <p className="text-sm font-medium text-gray-900 truncate">{filename}</p>
            <p className="text-xs text-gray-500 truncate">{directory}</p>
          </div>
        </div>
        <div className="flex items-center gap-1">
          <FireIcon className={`h-4 w-4 ${getRiskTextColor(file.risk_score)}`} />
          <span className={`text-sm font-semibold ${getRiskTextColor(file.risk_score)}`}>
            {file.risk_score}
          </span>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
        <div className="text-center p-2 bg-white/50 rounded">
          <p className="text-gray-500">Issues</p>
          <p className="font-semibold text-gray-900">{file.issues_count}</p>
        </div>
        <div className="text-center p-2 bg-white/50 rounded">
          <p className="text-gray-500">Complexity</p>
          <p className="font-semibold text-gray-900">{file.complexity}</p>
        </div>
        <div className="text-center p-2 bg-white/50 rounded">
          <p className="text-gray-500">Churn</p>
          <p className="font-semibold text-gray-900">{file.churn}</p>
        </div>
      </div>

      {file.authors.length > 0 && (
        <div className="mt-2 flex items-center gap-1 text-xs text-gray-500">
          <span>Contributors:</span>
          <span className="truncate">{file.authors.slice(0, 3).join(', ')}</span>
          {file.authors.length > 3 && (
            <span className="text-gray-400">+{file.authors.length - 3}</span>
          )}
        </div>
      )}
    </div>
  );
};

interface DirectoryHotspotCardProps {
  directory: DirectoryHotspot;
  rank: number;
}

const DirectoryHotspotCard: React.FC<DirectoryHotspotCardProps> = ({ directory, rank }) => {
  return (
    <div
      className={`p-4 rounded-lg border ${getRiskBgColor(directory.risk_score)} border-gray-200`}
    >
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2">
          <span
            className={`flex items-center justify-center w-6 h-6 rounded-full text-xs font-bold text-white ${getRiskColor(
              directory.risk_score
            )}`}
          >
            {rank}
          </span>
          <div className="flex items-center gap-1">
            <FolderIcon className="h-4 w-4 text-gray-400" />
            <p className="text-sm font-medium text-gray-900">{directory.path}</p>
          </div>
        </div>
        <div className="flex items-center gap-1">
          <FireIcon className={`h-4 w-4 ${getRiskTextColor(directory.risk_score)}`} />
          <span className={`text-sm font-semibold ${getRiskTextColor(directory.risk_score)}`}>
            {directory.risk_score}
          </span>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
        <div className="text-center p-2 bg-white/50 rounded">
          <p className="text-gray-500">Issues</p>
          <p className="font-semibold text-gray-900">{directory.total_issues}</p>
        </div>
        <div className="text-center p-2 bg-white/50 rounded">
          <p className="text-gray-500">Files</p>
          <p className="font-semibold text-gray-900">{directory.total_files}</p>
        </div>
        <div className="text-center p-2 bg-white/50 rounded">
          <p className="text-gray-500">Avg Complexity</p>
          <p className="font-semibold text-gray-900">{directory.average_complexity.toFixed(1)}</p>
        </div>
      </div>
    </div>
  );
};

interface TreemapCellProps {
  file: FileHotspot;
  style: React.CSSProperties;
  onClick?: () => void;
}

const TreemapCell: React.FC<TreemapCellProps> = ({ file, style, onClick }) => {
  const { filename } = formatPath(file.file_path);

  return (
    <div
      onClick={onClick}
      style={style}
      className={`${getRiskColor(file.risk_score)} p-2 cursor-pointer hover:opacity-90 transition-opacity overflow-hidden`}
      title={`${file.file_path}\nRisk: ${file.risk_score}\nIssues: ${file.issues_count}`}
    >
      <p className="text-xs text-white font-medium truncate">{filename}</p>
      <p className="text-xs text-white/80">{file.issues_count} issues</p>
    </div>
  );
};

// Simple treemap layout algorithm
const calculateTreemapLayout = (
  files: FileHotspot[],
  width: number,
  height: number
): { file: FileHotspot; x: number; y: number; width: number; height: number }[] => {
  const total = files.reduce((sum, f) => sum + f.risk_score, 0);
  const sorted = [...files].sort((a, b) => b.risk_score - a.risk_score);

  const result: { file: FileHotspot; x: number; y: number; width: number; height: number }[] = [];
  let currentX = 0;
  let currentY = 0;
  let rowHeight = 0;
  let rowWidth = width;

  sorted.forEach((file, index) => {
    const ratio = file.risk_score / total;
    const area = ratio * width * height;
    const cellWidth = Math.min(Math.sqrt(area * 1.5), rowWidth);
    const cellHeight = area / cellWidth;

    if (currentX + cellWidth > width) {
      currentX = 0;
      currentY += rowHeight;
      rowHeight = 0;
      rowWidth = width;
    }

    result.push({
      file,
      x: currentX,
      y: currentY,
      width: cellWidth,
      height: Math.max(cellHeight, 40),
    });

    currentX += cellWidth;
    rowHeight = Math.max(rowHeight, cellHeight);
  });

  return result;
};

const HotspotHeatmap: React.FC<HotspotHeatmapProps> = ({
  data,
  view = 'files',
  maxItems = 10,
  onFileClick,
}) => {
  const [currentView, setCurrentView] = React.useState(view);
  const containerRef = React.useRef<HTMLDivElement>(null);

  const sortedFiles = React.useMemo(() => {
    return [...data.files].sort((a, b) => b.risk_score - a.risk_score).slice(0, maxItems);
  }, [data.files, maxItems]);

  const sortedDirectories = React.useMemo(() => {
    return [...data.directories].sort((a, b) => b.risk_score - a.risk_score).slice(0, maxItems);
  }, [data.directories, maxItems]);

  if (data.files.length === 0 && data.directories.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 bg-gray-50 rounded-lg">
        <div className="text-center">
          <FireIcon className="mx-auto h-12 w-12 text-gray-400" />
          <p className="mt-2 text-gray-500">No hotspots detected</p>
          <p className="text-xs text-gray-400">All files have low risk scores</p>
        </div>
      </div>
    );
  }

  return (
    <div>
      {/* View Toggle */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex gap-2">
          <button
            onClick={() => setCurrentView('files')}
            className={`px-3 py-1.5 text-sm font-medium rounded-lg transition-colors ${
              currentView === 'files'
                ? 'bg-primary-100 text-primary-700'
                : 'text-gray-600 hover:bg-gray-100'
            }`}
          >
            <DocumentIcon className="inline-block w-4 h-4 mr-1" />
            Files
          </button>
          <button
            onClick={() => setCurrentView('directories')}
            className={`px-3 py-1.5 text-sm font-medium rounded-lg transition-colors ${
              currentView === 'directories'
                ? 'bg-primary-100 text-primary-700'
                : 'text-gray-600 hover:bg-gray-100'
            }`}
          >
            <FolderIcon className="inline-block w-4 h-4 mr-1" />
            Directories
          </button>
          <button
            onClick={() => setCurrentView('treemap')}
            className={`px-3 py-1.5 text-sm font-medium rounded-lg transition-colors ${
              currentView === 'treemap'
                ? 'bg-primary-100 text-primary-700'
                : 'text-gray-600 hover:bg-gray-100'
            }`}
          >
            <FireIcon className="inline-block w-4 h-4 mr-1" />
            Treemap
          </button>
        </div>

        {/* Legend */}
        <div className="flex items-center gap-2 text-xs">
          <span className="text-gray-500">Risk:</span>
          <div className="flex gap-1">
            <span className="w-4 h-4 rounded bg-green-500" title="Low (0-20)" />
            <span className="w-4 h-4 rounded bg-lime-500" title="Medium-Low (20-40)" />
            <span className="w-4 h-4 rounded bg-yellow-500" title="Medium (40-60)" />
            <span className="w-4 h-4 rounded bg-orange-500" title="High (60-80)" />
            <span className="w-4 h-4 rounded bg-red-500" title="Critical (80-100)" />
          </div>
        </div>
      </div>

      {/* Content */}
      {currentView === 'files' && (
        <div className="grid gap-3 sm:grid-cols-2">
          {sortedFiles.map((file, index) => (
            <FileHotspotCard
              key={file.file_path}
              file={file}
              rank={index + 1}
              onClick={() => onFileClick?.(file.file_path)}
            />
          ))}
        </div>
      )}

      {currentView === 'directories' && (
        <div className="grid gap-3 sm:grid-cols-2">
          {sortedDirectories.map((dir, index) => (
            <DirectoryHotspotCard key={dir.path} directory={dir} rank={index + 1} />
          ))}
        </div>
      )}

      {currentView === 'treemap' && (
        <div
          ref={containerRef}
          className="relative rounded-lg overflow-hidden border border-gray-200"
          style={{ height: 400 }}
        >
          {calculateTreemapLayout(sortedFiles, containerRef.current?.offsetWidth || 600, 400).map(
            (cell) => (
              <TreemapCell
                key={cell.file.file_path}
                file={cell.file}
                style={{
                  position: 'absolute',
                  left: cell.x,
                  top: cell.y,
                  width: cell.width - 2,
                  height: cell.height - 2,
                }}
                onClick={() => onFileClick?.(cell.file.file_path)}
              />
            )
          )}
        </div>
      )}
    </div>
  );
};

export default HotspotHeatmap;
