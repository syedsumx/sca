import axios, { AxiosInstance } from 'axios';
import {
  AnalysisResult,
  DashboardStats,
  ProjectSummary,
  DuplicationResult,
  DependencyScanResult,
  CoverageResult,
  Issue,
  CommitInfo,
  BlameInfo,
  ChangedFile,
} from '../types';

const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000/api/v1';

class ApiService {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      baseURL: API_BASE_URL,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // Request interceptor
    this.client.interceptors.request.use(
      (config) => {
        // Add auth token if available
        const token = localStorage.getItem('codescope_token');
        if (token) {
          config.headers.Authorization = `Bearer ${token}`;
        }
        return config;
      },
      (error) => Promise.reject(error)
    );

    // Response interceptor
    this.client.interceptors.response.use(
      (response) => response,
      (error) => {
        if (error.response?.status === 401) {
          localStorage.removeItem('codescope_token');
          window.location.href = '/login';
        }
        return Promise.reject(error);
      }
    );
  }

  // Dashboard
  async getDashboardStats(): Promise<DashboardStats> {
    const response = await this.client.get('/dashboard/stats');
    return response.data;
  }

  // Projects
  async getProjects(): Promise<ProjectSummary[]> {
    const response = await this.client.get('/projects');
    return response.data;
  }

  async getProject(projectId: string): Promise<ProjectSummary> {
    const response = await this.client.get(`/projects/${projectId}`);
    return response.data;
  }

  // Analysis
  async getAnalysis(analysisId: string): Promise<AnalysisResult> {
    const response = await this.client.get(`/analyses/${analysisId}`);
    return response.data;
  }

  async getLatestAnalysis(projectId: string): Promise<AnalysisResult> {
    const response = await this.client.get(`/projects/${projectId}/analyses/latest`);
    return response.data;
  }

  async getAnalysisHistory(projectId: string, limit = 10): Promise<AnalysisResult[]> {
    const response = await this.client.get(`/projects/${projectId}/analyses`, {
      params: { limit },
    });
    return response.data;
  }

  async triggerAnalysis(projectPath: string): Promise<{ analysis_id: string }> {
    const response = await this.client.post('/analyses', { path: projectPath });
    return response.data;
  }

  // Issues
  async getIssues(analysisId: string, params?: {
    severity?: string[];
    type?: string[];
    file?: string;
    page?: number;
    limit?: number;
  }): Promise<{ issues: Issue[]; total: number }> {
    const response = await this.client.get(`/analyses/${analysisId}/issues`, { params });
    return response.data;
  }

  async getIssuesByFile(analysisId: string, filePath: string): Promise<Issue[]> {
    const response = await this.client.get(`/analyses/${analysisId}/issues`, {
      params: { file: filePath },
    });
    return response.data.issues;
  }

  // Duplications
  async getDuplications(analysisId: string): Promise<DuplicationResult> {
    const response = await this.client.get(`/analyses/${analysisId}/duplications`);
    return response.data;
  }

  // Dependencies
  async getDependencies(analysisId: string): Promise<DependencyScanResult> {
    const response = await this.client.get(`/analyses/${analysisId}/dependencies`);
    return response.data;
  }

  // Coverage
  async getCoverage(analysisId: string): Promise<CoverageResult> {
    const response = await this.client.get(`/analyses/${analysisId}/coverage`);
    return response.data;
  }

  async getFileCoverage(analysisId: string, filePath: string): Promise<number[]> {
    const response = await this.client.get(`/analyses/${analysisId}/coverage/file`, {
      params: { path: filePath },
    });
    return response.data.uncovered_lines;
  }

  // Git
  async getCommitHistory(projectId: string, limit = 10): Promise<CommitInfo[]> {
    const response = await this.client.get(`/projects/${projectId}/git/commits`, {
      params: { limit },
    });
    return response.data;
  }

  async getBlame(projectId: string, filePath: string): Promise<BlameInfo[]> {
    const response = await this.client.get(`/projects/${projectId}/git/blame`, {
      params: { path: filePath },
    });
    return response.data;
  }

  async getChangedFiles(projectId: string, baseRef: string, headRef = 'HEAD'): Promise<ChangedFile[]> {
    const response = await this.client.get(`/projects/${projectId}/git/diff`, {
      params: { base: baseRef, head: headRef },
    });
    return response.data;
  }

  // Rules
  async getRules(): Promise<Array<{
    id: string;
    name: string;
    description: string;
    severity: string;
    type: string;
    language: string;
    tags: string[];
  }>> {
    const response = await this.client.get('/rules');
    return response.data;
  }

  // File content with issues
  async getFileWithIssues(analysisId: string, filePath: string): Promise<{
    content: string;
    issues: Issue[];
    coverage?: number[];
  }> {
    const response = await this.client.get(`/analyses/${analysisId}/files`, {
      params: { path: filePath },
    });
    return response.data;
  }

  // Export
  async exportReport(analysisId: string, format: 'json' | 'sarif' | 'html' | 'pdf'): Promise<Blob> {
    const response = await this.client.get(`/analyses/${analysisId}/export`, {
      params: { format },
      responseType: 'blob',
    });
    return response.data;
  }
}

export const api = new ApiService();
export default api;
