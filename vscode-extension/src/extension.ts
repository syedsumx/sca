import * as vscode from 'vscode';
import { CodeScopeScanner, ScanIssue, ScanResult } from './scanner';
import { CodeScopeCodeActionProvider, fetchAutoFix, addToIgnoreFile } from './codeactions';
import { StatusBarManager } from './statusbar';

/** Severity ordering for minimum-level filtering. */
const SEVERITY_ORDER: Record<string, number> = {
    INFO: 0,
    MINOR: 1,
    MAJOR: 2,
    CRITICAL: 3,
    BLOCKER: 4,
};

// ---------------------------------------------------------------------------
// Activation
// ---------------------------------------------------------------------------

let diagnosticCollection: vscode.DiagnosticCollection;
let outputChannel: vscode.OutputChannel;
let scanner: CodeScopeScanner;
let statusBar: StatusBarManager;

export function activate(context: vscode.ExtensionContext): void {
    outputChannel = vscode.window.createOutputChannel('CodeScope');
    outputChannel.appendLine('CodeScope extension activating...');

    diagnosticCollection = vscode.languages.createDiagnosticCollection('codescope');
    scanner = new CodeScopeScanner(outputChannel);
    statusBar = new StatusBarManager();

    const config = vscode.workspace.getConfiguration('codescope');
    if (!config.get<boolean>('enable', true)) {
        outputChannel.appendLine('CodeScope is disabled via settings.');
        statusBar.setVisible(false);
    }

    statusBar.setVisible(config.get<boolean>('statusBar.enabled', true));

    // ---- Register commands --------------------------------------------------

    context.subscriptions.push(
        vscode.commands.registerCommand('codescope.analyzeFile', () => {
            const editor = vscode.window.activeTextEditor;
            if (editor) {
                analyzeFile(editor.document);
            } else {
                vscode.window.showWarningMessage('No active file to analyze.');
            }
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('codescope.analyzeWorkspace', () => {
            analyzeWorkspace();
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('codescope.showRuleInfo', async (ruleId?: string) => {
            if (!ruleId) {
                ruleId = await vscode.window.showInputBox({
                    prompt: 'Enter CodeScope rule ID',
                    placeHolder: 'e.g. SEC-SQL-INJECT',
                });
            }
            if (ruleId) {
                showRuleInfo(ruleId);
            }
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand(
            'codescope.applyFix',
            async (uri?: vscode.Uri, diagnostic?: vscode.Diagnostic) => {
                if (!uri || !diagnostic) {
                    vscode.window.showWarningMessage('No fix context available.');
                    return;
                }
                await applyFix(uri, diagnostic);
            }
        )
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('codescope.ignoreRule', async () => {
            const editor = vscode.window.activeTextEditor;
            if (!editor) {
                return;
            }
            const diagnostics = diagnosticCollection
                .get(editor.document.uri)
                ?.filter((d) => d.range.intersection(editor.selection) !== undefined);

            if (!diagnostics || diagnostics.length === 0) {
                vscode.window.showInformationMessage('No CodeScope issues on the current line.');
                return;
            }

            for (const diag of diagnostics) {
                const ruleId = typeof diag.code === 'object'
                    ? String((diag.code as { value: string | number }).value)
                    : String(diag.code ?? '');
                if (ruleId) {
                    const edit = new vscode.WorkspaceEdit();
                    const line = editor.document.lineAt(diag.range.start.line);
                    const indent = line.text.match(/^\s*/)?.[0] ?? '';
                    edit.insert(
                        editor.document.uri,
                        new vscode.Position(diag.range.start.line, 0),
                        `${indent}# codescope:ignore ${ruleId}\n`
                    );
                    await vscode.workspace.applyEdit(edit);
                }
            }
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('codescope.ignoreRuleForFile', async (
            uri?: vscode.Uri,
            ruleId?: string
        ) => {
            if (!uri || !ruleId) {
                return;
            }
            const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
            if (workspaceRoot) {
                await addToIgnoreFile(workspaceRoot, uri.fsPath, ruleId);
                vscode.window.showInformationMessage(
                    `Added ${ruleId} ignore for ${uri.fsPath} to .codescopeignore`
                );
            }
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('codescope.toggleOnSave', () => {
            const cfg = vscode.workspace.getConfiguration('codescope');
            const current = cfg.get<boolean>('scanOnSave', true);
            cfg.update('scanOnSave', !current, vscode.ConfigurationTarget.Workspace);
            vscode.window.showInformationMessage(
                `CodeScope scan on save: ${!current ? 'enabled' : 'disabled'}`
            );
        })
    );

    // ---- Code action provider -----------------------------------------------

    const codeActionProvider = new CodeScopeCodeActionProvider(outputChannel);
    context.subscriptions.push(
        vscode.languages.registerCodeActionsProvider(
            { scheme: 'file' },
            codeActionProvider,
            {
                providedCodeActionKinds: CodeScopeCodeActionProvider.providedCodeActionKinds,
            }
        )
    );

    // ---- File watchers (scan on save / open) --------------------------------

    context.subscriptions.push(
        vscode.workspace.onDidSaveTextDocument((document) => {
            const cfg = vscode.workspace.getConfiguration('codescope');
            if (cfg.get<boolean>('enable', true) && cfg.get<boolean>('scanOnSave', true)) {
                analyzeFile(document);
            }
        })
    );

    context.subscriptions.push(
        vscode.workspace.onDidOpenTextDocument((document) => {
            const cfg = vscode.workspace.getConfiguration('codescope');
            if (cfg.get<boolean>('enable', true) && cfg.get<boolean>('scanOnOpen', true)) {
                analyzeFile(document);
            }
        })
    );

    context.subscriptions.push(
        vscode.workspace.onDidCloseTextDocument((document) => {
            diagnosticCollection.delete(document.uri);
            scanner.invalidateCache(document.uri.fsPath);
        })
    );

    // ---- Configuration change listener --------------------------------------

    context.subscriptions.push(
        vscode.workspace.onDidChangeConfiguration((e) => {
            if (e.affectsConfiguration('codescope.statusBar.enabled')) {
                const enabled = vscode.workspace
                    .getConfiguration('codescope')
                    .get<boolean>('statusBar.enabled', true);
                statusBar.setVisible(enabled);
            }
        })
    );

    // ---- Register disposables -----------------------------------------------

    context.subscriptions.push(diagnosticCollection);
    context.subscriptions.push(outputChannel);
    context.subscriptions.push({
        dispose: () => {
            scanner.dispose();
            statusBar.dispose();
        },
    });

    outputChannel.appendLine('CodeScope extension activated.');
}

// ---------------------------------------------------------------------------
// Deactivation
// ---------------------------------------------------------------------------

export function deactivate(): void {
    scanner?.dispose();
    statusBar?.dispose();
}

// ---------------------------------------------------------------------------
// Core analysis helpers
// ---------------------------------------------------------------------------

async function analyzeFile(document: vscode.TextDocument): Promise<void> {
    if (document.uri.scheme !== 'file') {
        return;
    }

    const config = vscode.workspace.getConfiguration('codescope');
    if (!config.get<boolean>('enable', true)) {
        return;
    }

    statusBar.showScanning();
    outputChannel.appendLine(`Analyzing file: ${document.uri.fsPath}`);

    try {
        const result = await scanner.scanFile(document.uri.fsPath);
        applyDiagnostics(document.uri, result);
        statusBar.updateFromDiagnostics(diagnosticCollection);
    } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        outputChannel.appendLine(`[error] ${message}`);
        statusBar.showError(message);
    }
}

async function analyzeWorkspace(): Promise<void> {
    const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
    if (!workspaceFolder) {
        vscode.window.showWarningMessage('No workspace folder open.');
        return;
    }

    statusBar.showScanning();
    outputChannel.appendLine(`Analyzing workspace: ${workspaceFolder.uri.fsPath}`);

    try {
        const result = await scanner.scanWorkspace(workspaceFolder.uri.fsPath);

        // Group issues by file
        const issuesByFile = new Map<string, ScanIssue[]>();
        for (const issue of result.issues) {
            const filePath = issue.location.file_path;
            if (!issuesByFile.has(filePath)) {
                issuesByFile.set(filePath, []);
            }
            issuesByFile.get(filePath)!.push(issue);
        }

        // Clear existing diagnostics and set new ones
        diagnosticCollection.clear();
        for (const [filePath, issues] of issuesByFile) {
            const uri = vscode.Uri.file(filePath);
            const fileResult: ScanResult = {
                files: [filePath],
                issues,
                metrics: result.metrics,
            };
            applyDiagnostics(uri, fileResult);
        }

        statusBar.updateFromDiagnostics(diagnosticCollection);

        vscode.window.showInformationMessage(
            `CodeScope: Found ${result.metrics.total_issues} issues in workspace.`
        );
    } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        outputChannel.appendLine(`[error] ${message}`);
        statusBar.showError(message);
        vscode.window.showErrorMessage(`CodeScope workspace scan failed: ${message}`);
    }
}

// ---------------------------------------------------------------------------
// Diagnostics mapping
// ---------------------------------------------------------------------------

function applyDiagnostics(uri: vscode.Uri, result: ScanResult): void {
    const config = vscode.workspace.getConfiguration('codescope');
    const minSeverity = config.get<string>('severity.minimum', 'MINOR');
    const minLevel = SEVERITY_ORDER[minSeverity] ?? 1;

    const diagnostics: vscode.Diagnostic[] = [];

    for (const issue of result.issues) {
        const issueLevel = SEVERITY_ORDER[issue.severity] ?? 0;
        if (issueLevel < minLevel) {
            continue;
        }

        const startLine = Math.max(0, (issue.location.start_line ?? 1) - 1);
        const endLine = Math.max(startLine, (issue.location.end_line ?? issue.location.start_line ?? 1) - 1);
        const startCol = Math.max(0, (issue.location.start_column ?? 1) - 1);
        const endCol = Math.max(startCol, (issue.location.end_column ?? 200) - 1);

        const range = new vscode.Range(startLine, startCol, endLine, endCol);

        const diagnostic = new vscode.Diagnostic(
            range,
            `${issue.message} [${issue.rule_id}]`,
            mapSeverity(issue.severity)
        );
        diagnostic.source = 'codescope';
        diagnostic.code = {
            value: issue.rule_id,
            target: vscode.Uri.parse(
                `https://codescope.dev/rules/${issue.rule_id}`
            ),
        };

        if (issue.cwe_ids.length > 0) {
            diagnostic.message += ` (CWE-${issue.cwe_ids.join(', CWE-')})`;
        }

        diagnostics.push(diagnostic);
    }

    diagnosticCollection.set(uri, diagnostics);
}

function mapSeverity(severity: string): vscode.DiagnosticSeverity {
    switch (severity) {
        case 'BLOCKER':
        case 'CRITICAL':
            return vscode.DiagnosticSeverity.Error;
        case 'MAJOR':
            return vscode.DiagnosticSeverity.Warning;
        case 'MINOR':
            return vscode.DiagnosticSeverity.Information;
        case 'INFO':
            return vscode.DiagnosticSeverity.Hint;
        default:
            return vscode.DiagnosticSeverity.Information;
    }
}

// ---------------------------------------------------------------------------
// Fix application
// ---------------------------------------------------------------------------

async function applyFix(uri: vscode.Uri, diagnostic: vscode.Diagnostic): Promise<void> {
    const ruleId = typeof diagnostic.code === 'object'
        ? String((diagnostic.code as { value: string | number }).value)
        : String(diagnostic.code ?? '');

    if (!ruleId) {
        vscode.window.showWarningMessage('Cannot determine rule ID for auto-fix.');
        return;
    }

    outputChannel.appendLine(`Requesting auto-fix for ${ruleId} at ${uri.fsPath}:${diagnostic.range.start.line + 1}`);

    const fix = await fetchAutoFix(
        uri.fsPath,
        ruleId,
        diagnostic.range.start.line + 1,
        outputChannel
    );

    if (!fix) {
        vscode.window.showInformationMessage(`No auto-fix available for ${ruleId}.`);
        return;
    }

    const edit = new vscode.WorkspaceEdit();
    edit.replace(uri, diagnostic.range, fix);

    const applied = await vscode.workspace.applyEdit(edit);
    if (applied) {
        vscode.window.showInformationMessage(`Applied fix for ${ruleId}.`);
        // Re-scan the file after applying fix
        const document = await vscode.workspace.openTextDocument(uri);
        analyzeFile(document);
    } else {
        vscode.window.showErrorMessage(`Failed to apply fix for ${ruleId}.`);
    }
}

// ---------------------------------------------------------------------------
// Rule info display
// ---------------------------------------------------------------------------

function showRuleInfo(ruleId: string): void {
    outputChannel.appendLine(`Showing rule info for: ${ruleId}`);
    outputChannel.show(true);
    outputChannel.appendLine('---');
    outputChannel.appendLine(`Rule: ${ruleId}`);
    outputChannel.appendLine(`Documentation: https://codescope.dev/rules/${ruleId}`);
    outputChannel.appendLine('---');

    // Open the rule documentation URL in the browser
    vscode.env.openExternal(
        vscode.Uri.parse(`https://codescope.dev/rules/${ruleId}`)
    );
}
