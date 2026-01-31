import * as vscode from 'vscode';
import { CodeScopeAnalyzer } from './analyzer';
import { IssueDiagnosticProvider } from './diagnostics';
import { IssueTreeProvider } from './issueTree';
import { CodeScopeCodeActionProvider } from './codeActions';

let analyzer: CodeScopeAnalyzer;
let diagnosticProvider: IssueDiagnosticProvider;
let issueTreeProvider: IssueTreeProvider;

export function activate(context: vscode.ExtensionContext) {
    console.log('CodeScope extension is now active');

    // Initialize components
    analyzer = new CodeScopeAnalyzer();
    diagnosticProvider = new IssueDiagnosticProvider();
    issueTreeProvider = new IssueTreeProvider();

    // Register diagnostics collection
    const diagnosticCollection = vscode.languages.createDiagnosticCollection('codescope');
    context.subscriptions.push(diagnosticCollection);
    diagnosticProvider.setDiagnosticCollection(diagnosticCollection);

    // Register tree view
    vscode.window.registerTreeDataProvider('codescopeIssues', issueTreeProvider);

    // Register code actions provider
    const codeActionProvider = new CodeScopeCodeActionProvider();
    context.subscriptions.push(
        vscode.languages.registerCodeActionsProvider(
            { scheme: 'file' },
            codeActionProvider,
            { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix] }
        )
    );

    // Register commands
    context.subscriptions.push(
        vscode.commands.registerCommand('codescope.analyze', () => analyzeCurrentFile()),
        vscode.commands.registerCommand('codescope.analyzeWorkspace', () => analyzeWorkspace()),
        vscode.commands.registerCommand('codescope.showIssues', () => showIssuesPanel()),
        vscode.commands.registerCommand('codescope.showDuplications', () => showDuplications()),
        vscode.commands.registerCommand('codescope.scanDependencies', () => scanDependencies()),
        vscode.commands.registerCommand('codescope.showCoverage', () => showCoverage()),
        vscode.commands.registerCommand('codescope.clearDiagnostics', () => clearDiagnostics())
    );

    // Register event handlers
    const config = vscode.workspace.getConfiguration('codescope');

    if (config.get('analyzeOnSave')) {
        context.subscriptions.push(
            vscode.workspace.onDidSaveTextDocument((document) => {
                if (shouldAnalyze(document)) {
                    analyzeDocument(document);
                }
            })
        );
    }

    if (config.get('analyzeOnChange')) {
        context.subscriptions.push(
            vscode.workspace.onDidChangeTextDocument((event) => {
                if (shouldAnalyze(event.document)) {
                    // Debounce analysis
                    debounceAnalyze(event.document);
                }
            })
        );
    }

    // Analyze open files on activation
    vscode.window.visibleTextEditors.forEach((editor) => {
        if (shouldAnalyze(editor.document)) {
            analyzeDocument(editor.document);
        }
    });
}

export function deactivate() {
    diagnosticProvider.clearAll();
}

// Analysis functions
async function analyzeCurrentFile() {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showWarningMessage('No active file to analyze');
        return;
    }

    await analyzeDocument(editor.document);
}

async function analyzeDocument(document: vscode.TextDocument) {
    const config = vscode.workspace.getConfiguration('codescope');
    if (!config.get('enable')) {
        return;
    }

    try {
        vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: 'CodeScope: Analyzing...',
                cancellable: false,
            },
            async () => {
                const issues = await analyzer.analyzeFile(document.uri.fsPath);
                diagnosticProvider.updateDiagnostics(document.uri, issues);
                issueTreeProvider.updateIssues(document.uri.fsPath, issues);
            }
        );
    } catch (error) {
        vscode.window.showErrorMessage(`CodeScope analysis failed: ${error}`);
    }
}

async function analyzeWorkspace() {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders) {
        vscode.window.showWarningMessage('No workspace folder open');
        return;
    }

    try {
        await vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: 'CodeScope: Analyzing workspace...',
                cancellable: true,
            },
            async (progress, token) => {
                for (const folder of workspaceFolders) {
                    if (token.isCancellationRequested) {
                        break;
                    }

                    progress.report({ message: `Analyzing ${folder.name}...` });

                    const results = await analyzer.analyzeDirectory(folder.uri.fsPath);

                    for (const [filePath, issues] of Object.entries(results)) {
                        const uri = vscode.Uri.file(filePath);
                        diagnosticProvider.updateDiagnostics(uri, issues);
                        issueTreeProvider.updateIssues(filePath, issues);
                    }
                }

                vscode.window.showInformationMessage('CodeScope: Workspace analysis complete');
            }
        );
    } catch (error) {
        vscode.window.showErrorMessage(`CodeScope workspace analysis failed: ${error}`);
    }
}

function showIssuesPanel() {
    vscode.commands.executeCommand('workbench.view.extension.codescope');
}

async function showDuplications() {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders) {
        vscode.window.showWarningMessage('No workspace folder open');
        return;
    }

    try {
        await vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: 'CodeScope: Detecting duplications...',
                cancellable: false,
            },
            async () => {
                const result = await analyzer.analyzeDuplications(workspaceFolders[0].uri.fsPath);

                // Show results in output channel
                const outputChannel = vscode.window.createOutputChannel('CodeScope Duplications');
                outputChannel.clear();
                outputChannel.appendLine('=== Code Duplication Analysis ===\n');
                outputChannel.appendLine(`Total duplicated lines: ${result.total_duplicated_lines}`);
                outputChannel.appendLine(`Duplication percentage: ${result.duplication_percentage.toFixed(1)}%`);
                outputChannel.appendLine(`Duplicate blocks: ${result.total_duplicated_blocks}\n`);

                for (const group of result.groups.slice(0, 10)) {
                    outputChannel.appendLine(`--- Duplication (${group.line_count} lines) ---`);
                    for (const block of group.blocks) {
                        outputChannel.appendLine(`  ${block.file_path}:${block.start_line}-${block.end_line}`);
                    }
                    outputChannel.appendLine('');
                }

                outputChannel.show();
            }
        );
    } catch (error) {
        vscode.window.showErrorMessage(`CodeScope duplication analysis failed: ${error}`);
    }
}

async function scanDependencies() {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders) {
        vscode.window.showWarningMessage('No workspace folder open');
        return;
    }

    try {
        await vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: 'CodeScope: Scanning dependencies...',
                cancellable: false,
            },
            async () => {
                const result = await analyzer.scanDependencies(workspaceFolders[0].uri.fsPath);

                const outputChannel = vscode.window.createOutputChannel('CodeScope Dependencies');
                outputChannel.clear();
                outputChannel.appendLine('=== Dependency Scan Results ===\n');
                outputChannel.appendLine(`Total dependencies: ${result.total_dependencies}`);
                outputChannel.appendLine(`Vulnerable packages: ${result.vulnerable_count}\n`);

                if (result.vulnerabilities.length > 0) {
                    outputChannel.appendLine('--- Vulnerabilities ---');
                    for (const vuln of result.vulnerabilities) {
                        outputChannel.appendLine(`\n[${vuln.severity}] ${vuln.id}`);
                        outputChannel.appendLine(`  Package: ${vuln.affected_package}`);
                        outputChannel.appendLine(`  Summary: ${vuln.summary}`);
                        if (vuln.fixed_version) {
                            outputChannel.appendLine(`  Fixed in: ${vuln.fixed_version}`);
                        }
                    }
                } else {
                    outputChannel.appendLine('No vulnerabilities found!');
                }

                outputChannel.show();
            }
        );
    } catch (error) {
        vscode.window.showErrorMessage(`CodeScope dependency scan failed: ${error}`);
    }
}

async function showCoverage() {
    vscode.window.showInformationMessage('CodeScope: Coverage visualization coming soon');
}

function clearDiagnostics() {
    diagnosticProvider.clearAll();
    issueTreeProvider.clear();
    vscode.window.showInformationMessage('CodeScope: Diagnostics cleared');
}

// Utility functions
function shouldAnalyze(document: vscode.TextDocument): boolean {
    const config = vscode.workspace.getConfiguration('codescope');
    if (!config.get('enable')) {
        return false;
    }

    const supportedLanguages = [
        'python', 'javascript', 'typescript', 'java', 'go',
        'csharp', 'ruby', 'php', 'rust', 'swift', 'kotlin', 'scala'
    ];

    if (!supportedLanguages.includes(document.languageId)) {
        return false;
    }

    const excludePatterns: string[] = config.get('excludePatterns') || [];
    for (const pattern of excludePatterns) {
        if (new RegExp(pattern.replace(/\*\*/g, '.*').replace(/\*/g, '[^/]*')).test(document.uri.fsPath)) {
            return false;
        }
    }

    return true;
}

let debounceTimer: NodeJS.Timeout | undefined;
function debounceAnalyze(document: vscode.TextDocument) {
    if (debounceTimer) {
        clearTimeout(debounceTimer);
    }
    debounceTimer = setTimeout(() => {
        analyzeDocument(document);
    }, 1000);
}
