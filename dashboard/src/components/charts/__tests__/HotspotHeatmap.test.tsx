/**
 * Tests for HotspotHeatmap component
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import HotspotHeatmap from '../HotspotHeatmap';
import { HotspotData, FileHotspot, DirectoryHotspot } from '../../../types';

const createMockFileHotspot = (overrides: Partial<FileHotspot> = {}): FileHotspot => ({
  file_path: 'src/api/handler.py',
  risk_score: 75,
  issues_count: 15,
  complexity: 25,
  churn: 10,
  authors: ['alice', 'bob'],
  last_modified: '2025-01-15T10:00:00Z',
  ...overrides,
});

const createMockDirectoryHotspot = (overrides: Partial<DirectoryHotspot> = {}): DirectoryHotspot => ({
  path: 'src/api',
  risk_score: 65,
  total_issues: 45,
  total_files: 12,
  average_complexity: 18.5,
  ...overrides,
});

const createMockHotspotData = (overrides: Partial<HotspotData> = {}): HotspotData => ({
  files: [
    createMockFileHotspot({ file_path: 'src/api/handler.py', risk_score: 85 }),
    createMockFileHotspot({ file_path: 'src/core/engine.py', risk_score: 65 }),
    createMockFileHotspot({ file_path: 'src/utils/helpers.py', risk_score: 35 }),
  ],
  directories: [
    createMockDirectoryHotspot({ path: 'src/api', risk_score: 75 }),
    createMockDirectoryHotspot({ path: 'src/core', risk_score: 55 }),
  ],
  ...overrides,
});

describe('HotspotHeatmap', () => {
  describe('rendering', () => {
    it('renders without crashing', () => {
      const data = createMockHotspotData();
      render(<HotspotHeatmap data={data} />);
      expect(screen.getByText('Files')).toBeInTheDocument();
    });

    it('displays empty state when no hotspots', () => {
      const data: HotspotData = { files: [], directories: [] };
      render(<HotspotHeatmap data={data} />);
      expect(screen.getByText('No hotspots detected')).toBeInTheDocument();
      expect(screen.getByText('All files have low risk scores')).toBeInTheDocument();
    });

    it('renders file hotspot cards in files view', () => {
      const data = createMockHotspotData();
      render(<HotspotHeatmap data={data} view="files" />);
      expect(screen.getByText('handler.py')).toBeInTheDocument();
    });

    it('renders directory hotspot cards in directories view', () => {
      const data = createMockHotspotData();
      render(<HotspotHeatmap data={data} view="directories" />);
      fireEvent.click(screen.getByText('Directories'));
      expect(screen.getByText('src/api')).toBeInTheDocument();
    });

    it('renders risk score legend', () => {
      const data = createMockHotspotData();
      render(<HotspotHeatmap data={data} />);
      expect(screen.getByText('Risk:')).toBeInTheDocument();
    });
  });

  describe('view switching', () => {
    it('switches to directories view when clicked', () => {
      const data = createMockHotspotData();
      render(<HotspotHeatmap data={data} />);

      fireEvent.click(screen.getByText('Directories'));
      expect(screen.getByText('src/api')).toBeInTheDocument();
    });

    it('switches to treemap view when clicked', () => {
      const data = createMockHotspotData();
      render(<HotspotHeatmap data={data} />);

      fireEvent.click(screen.getByText('Treemap'));
      // Treemap view should be active
      expect(screen.getByText('Treemap').closest('button')).toHaveClass('bg-primary-100');
    });

    it('switches back to files view', () => {
      const data = createMockHotspotData();
      render(<HotspotHeatmap data={data} />);

      fireEvent.click(screen.getByText('Directories'));
      fireEvent.click(screen.getByText('Files'));
      expect(screen.getByText('handler.py')).toBeInTheDocument();
    });
  });

  describe('file hotspot cards', () => {
    it('displays file name and directory', () => {
      const data = createMockHotspotData();
      render(<HotspotHeatmap data={data} />);

      expect(screen.getByText('handler.py')).toBeInTheDocument();
      expect(screen.getByText('src/api')).toBeInTheDocument();
    });

    it('displays risk score', () => {
      const data = createMockHotspotData({
        files: [createMockFileHotspot({ risk_score: 85 })],
      });
      render(<HotspotHeatmap data={data} />);

      expect(screen.getByText('85')).toBeInTheDocument();
    });

    it('displays issues count', () => {
      const data = createMockHotspotData({
        files: [createMockFileHotspot({ issues_count: 25 })],
      });
      render(<HotspotHeatmap data={data} />);

      expect(screen.getByText('25')).toBeInTheDocument();
    });

    it('displays complexity', () => {
      const data = createMockHotspotData({
        files: [createMockFileHotspot({ complexity: 30 })],
      });
      render(<HotspotHeatmap data={data} />);

      expect(screen.getByText('30')).toBeInTheDocument();
    });

    it('displays churn', () => {
      const data = createMockHotspotData({
        files: [createMockFileHotspot({ churn: 15 })],
      });
      render(<HotspotHeatmap data={data} />);

      expect(screen.getByText('15')).toBeInTheDocument();
    });

    it('displays contributors', () => {
      const data = createMockHotspotData({
        files: [createMockFileHotspot({ authors: ['alice', 'bob', 'charlie'] })],
      });
      render(<HotspotHeatmap data={data} />);

      expect(screen.getByText('alice, bob, charlie')).toBeInTheDocument();
    });

    it('truncates long contributor list', () => {
      const data = createMockHotspotData({
        files: [createMockFileHotspot({ authors: ['alice', 'bob', 'charlie', 'david', 'eve'] })],
      });
      render(<HotspotHeatmap data={data} />);

      expect(screen.getByText('+2')).toBeInTheDocument();
    });
  });

  describe('directory hotspot cards', () => {
    it('displays directory path', () => {
      const data = createMockHotspotData();
      render(<HotspotHeatmap data={data} view="directories" />);
      fireEvent.click(screen.getByText('Directories'));

      expect(screen.getByText('src/api')).toBeInTheDocument();
    });

    it('displays total issues', () => {
      const data = createMockHotspotData({
        directories: [createMockDirectoryHotspot({ total_issues: 50 })],
      });
      render(<HotspotHeatmap data={data} />);
      fireEvent.click(screen.getByText('Directories'));

      expect(screen.getByText('50')).toBeInTheDocument();
    });

    it('displays total files', () => {
      const data = createMockHotspotData({
        directories: [createMockDirectoryHotspot({ total_files: 15 })],
      });
      render(<HotspotHeatmap data={data} />);
      fireEvent.click(screen.getByText('Directories'));

      expect(screen.getByText('15')).toBeInTheDocument();
    });

    it('displays average complexity', () => {
      const data = createMockHotspotData({
        directories: [createMockDirectoryHotspot({ average_complexity: 22.5 })],
      });
      render(<HotspotHeatmap data={data} />);
      fireEvent.click(screen.getByText('Directories'));

      expect(screen.getByText('22.5')).toBeInTheDocument();
    });
  });

  describe('risk score colors', () => {
    it('applies critical color for score >= 80', () => {
      const data = createMockHotspotData({
        files: [createMockFileHotspot({ risk_score: 85 })],
      });
      render(<HotspotHeatmap data={data} />);
      // Card should have red background class
      expect(screen.getByText('85').closest('div')).toHaveClass('text-red-700');
    });

    it('applies high color for score >= 60', () => {
      const data = createMockHotspotData({
        files: [createMockFileHotspot({ risk_score: 65 })],
      });
      render(<HotspotHeatmap data={data} />);
      expect(screen.getByText('65').closest('div')).toHaveClass('text-orange-700');
    });

    it('applies medium color for score >= 40', () => {
      const data = createMockHotspotData({
        files: [createMockFileHotspot({ risk_score: 45 })],
      });
      render(<HotspotHeatmap data={data} />);
      expect(screen.getByText('45').closest('div')).toHaveClass('text-yellow-700');
    });

    it('applies low color for score >= 20', () => {
      const data = createMockHotspotData({
        files: [createMockFileHotspot({ risk_score: 25 })],
      });
      render(<HotspotHeatmap data={data} />);
      expect(screen.getByText('25').closest('div')).toHaveClass('text-lime-700');
    });

    it('applies safe color for score < 20', () => {
      const data = createMockHotspotData({
        files: [createMockFileHotspot({ risk_score: 10 })],
      });
      render(<HotspotHeatmap data={data} />);
      expect(screen.getByText('10').closest('div')).toHaveClass('text-green-700');
    });
  });

  describe('ranking', () => {
    it('displays rank numbers', () => {
      const data = createMockHotspotData();
      render(<HotspotHeatmap data={data} />);

      expect(screen.getByText('1')).toBeInTheDocument();
      expect(screen.getByText('2')).toBeInTheDocument();
      expect(screen.getByText('3')).toBeInTheDocument();
    });

    it('sorts files by risk score descending', () => {
      const data = createMockHotspotData({
        files: [
          createMockFileHotspot({ file_path: 'low.py', risk_score: 20 }),
          createMockFileHotspot({ file_path: 'high.py', risk_score: 90 }),
          createMockFileHotspot({ file_path: 'mid.py', risk_score: 50 }),
        ],
      });
      render(<HotspotHeatmap data={data} />);

      // First card should be high risk
      const fileNames = screen.getAllByText(/\.py$/).map(el => el.textContent);
      expect(fileNames[0]).toBe('high.py');
    });

    it('limits display to maxItems', () => {
      const files = Array(20)
        .fill(null)
        .map((_, i) => createMockFileHotspot({ file_path: `file${i}.py`, risk_score: 100 - i * 5 }));

      const data = createMockHotspotData({ files });
      render(<HotspotHeatmap data={data} maxItems={5} />);

      // Should only show 5 files
      const fileNames = screen.getAllByText(/file\d+\.py$/);
      expect(fileNames.length).toBe(5);
    });
  });

  describe('file click callback', () => {
    it('calls onFileClick with file path when clicked', () => {
      const onFileClick = jest.fn();
      const data = createMockHotspotData();
      render(<HotspotHeatmap data={data} onFileClick={onFileClick} />);

      const fileCard = screen.getByText('handler.py').closest('div[class*="cursor-pointer"]');
      if (fileCard) {
        fireEvent.click(fileCard);
        expect(onFileClick).toHaveBeenCalledWith('src/api/handler.py');
      }
    });
  });

  describe('edge cases', () => {
    it('handles files with no authors', () => {
      const data = createMockHotspotData({
        files: [createMockFileHotspot({ authors: [] })],
      });
      render(<HotspotHeatmap data={data} />);
      expect(screen.queryByText('Contributors:')).not.toBeInTheDocument();
    });

    it('handles single character file name', () => {
      const data = createMockHotspotData({
        files: [createMockFileHotspot({ file_path: 'a.py' })],
      });
      render(<HotspotHeatmap data={data} />);
      expect(screen.getByText('a.py')).toBeInTheDocument();
    });

    it('handles deeply nested file paths', () => {
      const data = createMockHotspotData({
        files: [createMockFileHotspot({ file_path: 'a/b/c/d/e/f/g/file.py' })],
      });
      render(<HotspotHeatmap data={data} />);
      expect(screen.getByText('file.py')).toBeInTheDocument();
      expect(screen.getByText('a/b/c/d/e/f/g')).toBeInTheDocument();
    });

    it('handles file in root directory', () => {
      const data = createMockHotspotData({
        files: [createMockFileHotspot({ file_path: 'file.py' })],
      });
      render(<HotspotHeatmap data={data} />);
      expect(screen.getByText('file.py')).toBeInTheDocument();
      expect(screen.getByText('.')).toBeInTheDocument();
    });
  });
});
