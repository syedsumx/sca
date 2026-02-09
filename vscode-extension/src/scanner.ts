import * as vscode from 'vscode';
import { spawn, ChildProcess } from 'child_process';

/**
 * Represents a single issue found by CodeScope scanning.
 */
export interface ScanIssue {
    rule_id: string;
    rule_name: string;
    message: string;
    severity: string;
    issue_type: string;
    location: {
        file_path: string;
        start_line: number;
        end_line: number;
        start_column: number;
        end_column: number;
        snippet: string;
    };
    cwe_ids: number[];
    effort_minutes: number;
}

/**
 * Aggregated scan result returned by the CodeScope CLI.
 */
export interface ScanResult {
    files: string[];
    issues: ScanIssue[];
    metrics: {
        total_issues: number;
        vulnerabilities_count: number;
        bugs_count: number;
        code_smells_count: number;
    };
}

interface CacheEntry {
    result: ScanResult;
    timestamp: number;
}

const DEFAULT_TIMEOUT_MS = 30_000;

/**
 * Manages spawning the CodeScope CLI for scanning files and workspaces.
 * Handles JSON output parsing, timeouts, caching, and cancellation.
 */
export class CodeScopeScanner {
    private cache: Map<string, CacheEntry> = new Map();
    private activeProcesses: Map<string, ChildProcess> = new Map();
    private outputChannel: vscode.OutputChannel;

    constructor(outputChannel: vscode.OutputChannel) {
        this.outputChannel = outputChannel;
    }

    /**
     * Scan a single file and return parsed results.
     */
    public async scanFile(
        filePath: string,
        token?: vscode.CancellationToken
    ): Promise<ScanResult> {
        const cached = this.cache.get(filePath);
        if (cached && Date.now() - cached.timestamp < 10_000) {
            this.outputChannel.appendLine(`[cache hit] ${filePath}`);
            return cached.result;
        }

        const config = vscode.workspace.getConfiguration('codescope');
        const pythonPath = config.get<string>('python.path', 'python3');
        const disabledRules = config.get<string[]>('rules.disabled', []);

        const args = ['-m', 'codescope.cli', 'scan', filePath, '--format', 'json'];
        if (disabledRules.length > 0) {
            args.push('--disable-rules', disabledRules.join(','));
        }

        const result = await this.runProcess(pythonPath, args, filePath, token);
        this.cache.set(filePath, { result, timestamp: Date.now() });
        return result;
    }

    /**
     * Scan an entire workspace directory.
     */
    public async scanWorkspace(
        workspacePath: string,
        token?: vscode.CancellationToken
    ): Promise<ScanResult> {
        const config = vscode.workspace.getConfiguration('codescope');
        const pythonPath = config.get<string>('python.path', 'python3');
        const disabledRules = config.get<string[]>('rules.disabled', []);

        const args = ['-m', 'codescope.cli', 'scan', workspacePath, '--format', 'json'];
        if (disabledRules.length > 0) {
            args.push('--disable-rules', disabledRules.join(','));
        }

        return this.runProcess(pythonPath, args, workspacePath, token);
    }

    /**
     * Invalidate the cache entry for a given file path.
     */
    public invalidateCache(filePath: string): void {
        this.cache.delete(filePath);
    }

    /**
     * Clear the entire scan cache.
     */
    public clearCache(): void {
        this.cache.clear();
    }

    /**
     * Cancel any active scan for the given key.
     */
    public cancelScan(key: string): void {
        const proc = this.activeProcesses.get(key);
        if (proc && !proc.killed) {
            proc.kill('SIGTERM');
            this.activeProcesses.delete(key);
            this.outputChannel.appendLine(`[cancelled] ${key}`);
        }
    }

    /**
     * Cancel all active scans.
     */
    public cancelAll(): void {
        for (const [key, proc] of this.activeProcesses) {
            if (!proc.killed) {
                proc.kill('SIGTERM');
            }
            this.activeProcesses.delete(key);
        }
    }

    /**
     * Dispose of all resources.
     */
    public dispose(): void {
        this.cancelAll();
        this.clearCache();
    }

    private runProcess(
        pythonPath: string,
        args: string[],
        key: string,
        token?: vscode.CancellationToken
    ): Promise<ScanResult> {
        return new Promise<ScanResult>((resolve, reject) => {
            // Cancel any existing process for this key
            this.cancelScan(key);

            this.outputChannel.appendLine(`[scan] ${pythonPath} ${args.join(' ')}`);

            const proc = spawn(pythonPath, args, {
                cwd: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath,
                env: { ...process.env },
            });

            this.activeProcesses.set(key, proc);

            let stdout = '';
            let stderr = '';

            proc.stdout.on('data', (data: Buffer) => {
                stdout += data.toString();
            });

            proc.stderr.on('data', (data: Buffer) => {
                stderr += data.toString();
            });

            // Set up timeout
            const timeout = setTimeout(() => {
                if (!proc.killed) {
                    proc.kill('SIGTERM');
                    this.activeProcesses.delete(key);
                    reject(new Error(`CodeScope scan timed out after ${DEFAULT_TIMEOUT_MS / 1000}s`));
                }
            }, DEFAULT_TIMEOUT_MS);

            // Handle cancellation token
            const tokenDisposable = token?.onCancellationRequested(() => {
                clearTimeout(timeout);
                if (!proc.killed) {
                    proc.kill('SIGTERM');
                    this.activeProcesses.delete(key);
                    reject(new Error('Scan cancelled by user'));
                }
            });

            proc.on('close', (code: number | null) => {
                clearTimeout(timeout);
                tokenDisposable?.dispose();
                this.activeProcesses.delete(key);

                if (stderr) {
                    this.outputChannel.appendLine(`[stderr] ${stderr.trim()}`);
                }

                if (code !== 0 && code !== null) {
                    // CodeScope may exit with non-zero when issues are found; try parsing anyway
                    this.outputChannel.appendLine(
                        `[warn] Process exited with code ${code}`
                    );
                }

                try {
                    const result = this.parseOutput(stdout);
                    resolve(result);
                } catch (err) {
                    reject(
                        new Error(
                            `Failed to parse CodeScope output: ${err instanceof Error ? err.message : String(err)}`
                        )
                    );
                }
            });

            proc.on('error', (err: Error) => {
                clearTimeout(timeout);
                tokenDisposable?.dispose();
                this.activeProcesses.delete(key);
                reject(new Error(`Failed to launch CodeScope: ${err.message}`));
            });
        });
    }

    private parseOutput(raw: string): ScanResult {
        const trimmed = raw.trim();
        if (!trimmed) {
            return {
                files: [],
                issues: [],
                metrics: {
                    total_issues: 0,
                    vulnerabilities_count: 0,
                    bugs_count: 0,
                    code_smells_count: 0,
                },
            };
        }

        const parsed = JSON.parse(trimmed);

        // Normalise to our ScanResult shape regardless of minor CLI output variations
        const issues: ScanIssue[] = (parsed.issues ?? []).map((i: Record<string, unknown>) => ({
            rule_id: i.rule_id ?? '',
            rule_name: i.rule_name ?? '',
            message: i.message ?? '',
            severity: i.severity ?? 'INFO',
            issue_type: i.issue_type ?? 'CODE_SMELL',
            location: {
                file_path: (i.location as Record<string, unknown>)?.file_path ?? '',
                start_line: (i.location as Record<string, unknown>)?.start_line ?? 1,
                end_line: (i.location as Record<string, unknown>)?.end_line ?? 1,
                start_column: (i.location as Record<string, unknown>)?.start_column ?? 0,
                end_column: (i.location as Record<string, unknown>)?.end_column ?? 0,
                snippet: (i.location as Record<string, unknown>)?.snippet ?? '',
            },
            cwe_ids: (i.cwe_ids as number[]) ?? [],
            effort_minutes: (i.effort_minutes as number) ?? 0,
        }));

        return {
            files: parsed.files ?? [],
            issues,
            metrics: {
                total_issues: parsed.metrics?.total_issues ?? issues.length,
                vulnerabilities_count: parsed.metrics?.vulnerabilities_count ?? 0,
                bugs_count: parsed.metrics?.bugs_count ?? 0,
                code_smells_count: parsed.metrics?.code_smells_count ?? 0,
            },
        };
    }
}
