import * as vscode from 'vscode';
import { spawn } from 'child_process';
import axios from 'axios';

export interface Issue {
    id: string;
    rule_id: string;
    rule_name: string;
    severity: 'BLOCKER' | 'CRITICAL' | 'MAJOR' | 'MINOR' | 'INFO';
    issue_type: 'BUG' | 'VULNERABILITY' | 'CODE_SMELL' | 'SECURITY_HOTSPOT';
    message: string;
    file_path: string;
    start_line: number;
    end_line: number;
    start_column?: number;
    end_column?: number;
    snippet?: string;
    suggestion?: string;
}

export interface DuplicationResult {
    total_duplicated_lines: number;
    total_duplicated_blocks: number;
    duplication_percentage: number;
    groups: DuplicationGroup[];
}

export interface DuplicationGroup {
    id: string;
    fingerprint: string;
    token_count: number;
    line_count: number;
    blocks: DuplicateBlock[];
}

export interface DuplicateBlock {
    file_path: string;
    start_line: number;
    end_line: number;
    lines: number;
}

export interface DependencyScanResult {
    total_dependencies: number;
    vulnerable_count: number;
    dependencies: Dependency[];
    vulnerabilities: Vulnerability[];
}

export interface Dependency {
    name: string;
    version: string;
    ecosystem: string;
}

export interface Vulnerability {
    id: string;
    severity: string;
    summary: string;
    affected_package: string;
    fixed_version?: string;
}

export class CodeScopeAnalyzer {
    private pythonPath: string;
    private serverUrl: string | undefined;

    constructor() {
        const config = vscode.workspace.getConfiguration('codescope');
        this.pythonPath = config.get('pythonPath') || 'python';
        this.serverUrl = config.get('serverUrl') || undefined;
    }

    async analyzeFile(filePath: string): Promise<Issue[]> {
        if (this.serverUrl) {
            return this.analyzeViaServer(filePath);
        }
        return this.analyzeViaCli(filePath);
    }

    async analyzeDirectory(dirPath: string): Promise<Record<string, Issue[]>> {
        if (this.serverUrl) {
            return this.analyzeDirectoryViaServer(dirPath);
        }
        return this.analyzeDirectoryViaCli(dirPath);
    }

    async analyzeDuplications(dirPath: string): Promise<DuplicationResult> {
        if (this.serverUrl) {
            return this.analyzeDuplicationsViaServer(dirPath);
        }
        return this.analyzeDuplicationsViaCli(dirPath);
    }

    async scanDependencies(dirPath: string): Promise<DependencyScanResult> {
        if (this.serverUrl) {
            return this.scanDependenciesViaServer(dirPath);
        }
        return this.scanDependenciesViaCli(dirPath);
    }

    // CLI-based analysis
    private async analyzeViaCli(filePath: string): Promise<Issue[]> {
        return new Promise((resolve, reject) => {
            const args = ['-m', 'codescope', 'scan', filePath, '--format', 'json'];
            const process = spawn(this.pythonPath, args);

            let stdout = '';
            let stderr = '';

            process.stdout.on('data', (data) => {
                stdout += data.toString();
            });

            process.stderr.on('data', (data) => {
                stderr += data.toString();
            });

            process.on('close', (code) => {
                if (code !== 0 && code !== 1) {
                    reject(new Error(`Analysis failed: ${stderr}`));
                    return;
                }

                try {
                    const result = JSON.parse(stdout);
                    const issues: Issue[] = (result.issues || []).map((issue: any) => ({
                        id: issue.id,
                        rule_id: issue.rule_id,
                        rule_name: issue.rule_name,
                        severity: issue.severity,
                        issue_type: issue.issue_type,
                        message: issue.message,
                        file_path: issue.location?.file_path || filePath,
                        start_line: issue.location?.start_line || 1,
                        end_line: issue.location?.end_line || 1,
                        start_column: issue.location?.start_column,
                        end_column: issue.location?.end_column,
                        snippet: issue.snippet,
                        suggestion: issue.suggestion,
                    }));
                    resolve(issues);
                } catch (error) {
                    reject(new Error(`Failed to parse analysis result: ${error}`));
                }
            });
        });
    }

    private async analyzeDirectoryViaCli(dirPath: string): Promise<Record<string, Issue[]>> {
        return new Promise((resolve, reject) => {
            const args = ['-m', 'codescope', 'scan', dirPath, '--format', 'json'];
            const process = spawn(this.pythonPath, args);

            let stdout = '';
            let stderr = '';

            process.stdout.on('data', (data) => {
                stdout += data.toString();
            });

            process.stderr.on('data', (data) => {
                stderr += data.toString();
            });

            process.on('close', (code) => {
                if (code !== 0 && code !== 1) {
                    reject(new Error(`Analysis failed: ${stderr}`));
                    return;
                }

                try {
                    const result = JSON.parse(stdout);
                    const issuesByFile: Record<string, Issue[]> = {};

                    for (const issue of result.issues || []) {
                        const filePath = issue.location?.file_path || 'unknown';
                        if (!issuesByFile[filePath]) {
                            issuesByFile[filePath] = [];
                        }
                        issuesByFile[filePath].push({
                            id: issue.id,
                            rule_id: issue.rule_id,
                            rule_name: issue.rule_name,
                            severity: issue.severity,
                            issue_type: issue.issue_type,
                            message: issue.message,
                            file_path: filePath,
                            start_line: issue.location?.start_line || 1,
                            end_line: issue.location?.end_line || 1,
                            start_column: issue.location?.start_column,
                            end_column: issue.location?.end_column,
                            snippet: issue.snippet,
                            suggestion: issue.suggestion,
                        });
                    }

                    resolve(issuesByFile);
                } catch (error) {
                    reject(new Error(`Failed to parse analysis result: ${error}`));
                }
            });
        });
    }

    private async analyzeDuplicationsViaCli(dirPath: string): Promise<DuplicationResult> {
        return new Promise((resolve, reject) => {
            const args = ['-m', 'codescope', 'duplications', dirPath, '--format', 'json'];
            const process = spawn(this.pythonPath, args);

            let stdout = '';
            let stderr = '';

            process.stdout.on('data', (data) => {
                stdout += data.toString();
            });

            process.stderr.on('data', (data) => {
                stderr += data.toString();
            });

            process.on('close', (code) => {
                if (code !== 0) {
                    reject(new Error(`Duplication analysis failed: ${stderr}`));
                    return;
                }

                try {
                    const result = JSON.parse(stdout);
                    resolve(result);
                } catch (error) {
                    reject(new Error(`Failed to parse duplication result: ${error}`));
                }
            });
        });
    }

    private async scanDependenciesViaCli(dirPath: string): Promise<DependencyScanResult> {
        return new Promise((resolve, reject) => {
            const args = ['-m', 'codescope', 'dependencies', dirPath, '--format', 'json'];
            const process = spawn(this.pythonPath, args);

            let stdout = '';
            let stderr = '';

            process.stdout.on('data', (data) => {
                stdout += data.toString();
            });

            process.stderr.on('data', (data) => {
                stderr += data.toString();
            });

            process.on('close', (code) => {
                if (code !== 0) {
                    reject(new Error(`Dependency scan failed: ${stderr}`));
                    return;
                }

                try {
                    const result = JSON.parse(stdout);
                    resolve(result);
                } catch (error) {
                    reject(new Error(`Failed to parse dependency scan result: ${error}`));
                }
            });
        });
    }

    // Server-based analysis
    private async analyzeViaServer(filePath: string): Promise<Issue[]> {
        const response = await axios.post(`${this.serverUrl}/api/v1/analyses`, {
            path: filePath,
        });

        const analysisId = response.data.analysis_id;

        // Poll for results
        let result;
        for (let i = 0; i < 30; i++) {
            await new Promise((resolve) => setTimeout(resolve, 1000));
            try {
                const statusResponse = await axios.get(
                    `${this.serverUrl}/api/v1/analyses/${analysisId}/issues`
                );
                result = statusResponse.data;
                break;
            } catch (error: any) {
                if (error.response?.status !== 202) {
                    throw error;
                }
            }
        }

        return result?.issues || [];
    }

    private async analyzeDirectoryViaServer(dirPath: string): Promise<Record<string, Issue[]>> {
        const response = await axios.post(`${this.serverUrl}/api/v1/analyses`, {
            path: dirPath,
        });

        const analysisId = response.data.analysis_id;

        // Poll for results
        let issues: Issue[] = [];
        for (let i = 0; i < 60; i++) {
            await new Promise((resolve) => setTimeout(resolve, 1000));
            try {
                const statusResponse = await axios.get(
                    `${this.serverUrl}/api/v1/analyses/${analysisId}/issues`
                );
                issues = statusResponse.data.issues || [];
                break;
            } catch (error: any) {
                if (error.response?.status !== 202) {
                    throw error;
                }
            }
        }

        // Group by file
        const issuesByFile: Record<string, Issue[]> = {};
        for (const issue of issues) {
            const filePath = issue.file_path || 'unknown';
            if (!issuesByFile[filePath]) {
                issuesByFile[filePath] = [];
            }
            issuesByFile[filePath].push(issue);
        }

        return issuesByFile;
    }

    private async analyzeDuplicationsViaServer(dirPath: string): Promise<DuplicationResult> {
        const response = await axios.post(`${this.serverUrl}/api/v1/duplications/analyze`, {
            path: dirPath,
        });
        return response.data;
    }

    private async scanDependenciesViaServer(dirPath: string): Promise<DependencyScanResult> {
        const response = await axios.post(`${this.serverUrl}/api/v1/dependencies/scan`, {
            path: dirPath,
        });
        return response.data;
    }
}
