import * as vscode from 'vscode';
import { spawn } from 'child_process';
import * as path from 'path';

/**
 * Provides code actions for CodeScope diagnostics:
 * - Apply auto-fix suggestion
 * - Ignore rule for this line
 * - Ignore rule for this file
 * - Show rule details
 */
export class CodeScopeCodeActionProvider implements vscode.CodeActionProvider {
    public static readonly providedCodeActionKinds = [
        vscode.CodeActionKind.QuickFix,
        vscode.CodeActionKind.Source,
    ];

    private outputChannel: vscode.OutputChannel;

    constructor(outputChannel: vscode.OutputChannel) {
        this.outputChannel = outputChannel;
    }

    public provideCodeActions(
        document: vscode.TextDocument,
        range: vscode.Range | vscode.Selection,
        context: vscode.CodeActionContext,
        _token: vscode.CancellationToken
    ): vscode.CodeAction[] {
        const actions: vscode.CodeAction[] = [];
        const config = vscode.workspace.getConfiguration('codescope');
        const autoFixEnabled = config.get<boolean>('autoFix.enabled', true);

        for (const diagnostic of context.diagnostics) {
            if (diagnostic.source !== 'codescope') {
                continue;
            }

            const ruleId = typeof diagnostic.code === 'object'
                ? String((diagnostic.code as { value: string | number }).value)
                : String(diagnostic.code ?? '');

            if (!ruleId) {
                continue;
            }

            // 1. Apply auto-fix
            if (autoFixEnabled) {
                const fixAction = new vscode.CodeAction(
                    `Apply CodeScope fix for ${ruleId}`,
                    vscode.CodeActionKind.QuickFix
                );
                fixAction.command = {
                    command: 'codescope.applyFix',
                    title: 'Apply CodeScope Auto-Fix',
                    arguments: [document.uri, diagnostic],
                };
                fixAction.diagnostics = [diagnostic];
                fixAction.isPreferred = true;
                actions.push(fixAction);
            }

            // 2. Ignore rule for this line
            const ignoreLine = new vscode.CodeAction(
                `Ignore ${ruleId} for this line`,
                vscode.CodeActionKind.QuickFix
            );
            ignoreLine.edit = this.createIgnoreLineEdit(document, diagnostic.range, ruleId);
            ignoreLine.diagnostics = [diagnostic];
            actions.push(ignoreLine);

            // 3. Ignore rule for this file
            const ignoreFile = new vscode.CodeAction(
                `Ignore ${ruleId} for this file`,
                vscode.CodeActionKind.Source
            );
            ignoreFile.command = {
                command: 'codescope.ignoreRuleForFile',
                title: 'Ignore Rule for File',
                arguments: [document.uri, ruleId],
            };
            ignoreFile.diagnostics = [diagnostic];
            actions.push(ignoreFile);

            // 4. Show rule details
            const showInfo = new vscode.CodeAction(
                `Show details for ${ruleId}`,
                vscode.CodeActionKind.Source
            );
            showInfo.command = {
                command: 'codescope.showRuleInfo',
                title: 'Show Rule Info',
                arguments: [ruleId],
            };
            showInfo.diagnostics = [diagnostic];
            actions.push(showInfo);
        }

        return actions;
    }

    /**
     * Creates a WorkspaceEdit that inserts a `# codescope:ignore RULE_ID` comment
     * on the line above the diagnostic.
     */
    private createIgnoreLineEdit(
        document: vscode.TextDocument,
        range: vscode.Range,
        ruleId: string
    ): vscode.WorkspaceEdit {
        const edit = new vscode.WorkspaceEdit();
        const line = document.lineAt(range.start.line);
        const indent = line.text.match(/^\s*/)?.[0] ?? '';
        const comment = `${indent}# codescope:ignore ${ruleId}\n`;
        edit.insert(document.uri, new vscode.Position(range.start.line, 0), comment);
        return edit;
    }
}

/**
 * Run the CodeScope auto-fix CLI for a specific file and rule, returning the
 * suggested fix text (if any).
 */
export function fetchAutoFix(
    filePath: string,
    ruleId: string,
    line: number,
    outputChannel: vscode.OutputChannel
): Promise<string | null> {
    return new Promise<string | null>((resolve) => {
        const config = vscode.workspace.getConfiguration('codescope');
        const pythonPath = config.get<string>('python.path', 'python3');

        const args = [
            '-m',
            'codescope.cli',
            'autofix',
            'suggest',
            filePath,
            '--rule',
            ruleId,
            '--line',
            String(line),
            '--format',
            'json',
        ];

        outputChannel.appendLine(`[autofix] ${pythonPath} ${args.join(' ')}`);

        const proc = spawn(pythonPath, args, {
            cwd: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath,
        });

        let stdout = '';
        let stderr = '';

        proc.stdout.on('data', (data: Buffer) => {
            stdout += data.toString();
        });

        proc.stderr.on('data', (data: Buffer) => {
            stderr += data.toString();
        });

        proc.on('close', (code: number | null) => {
            if (stderr) {
                outputChannel.appendLine(`[autofix stderr] ${stderr.trim()}`);
            }
            if (code !== 0) {
                outputChannel.appendLine(`[autofix] exited with code ${code}`);
                resolve(null);
                return;
            }
            try {
                const result = JSON.parse(stdout.trim());
                resolve(result.fix ?? result.suggestion ?? null);
            } catch {
                resolve(null);
            }
        });

        proc.on('error', (err: Error) => {
            outputChannel.appendLine(`[autofix error] ${err.message}`);
            resolve(null);
        });
    });
}

/**
 * Appends an ignore entry to the workspace `.codescopeignore` file.
 */
export async function addToIgnoreFile(
    workspaceRoot: string,
    filePath: string,
    ruleId: string
): Promise<void> {
    const ignoreFilePath = path.join(workspaceRoot, '.codescopeignore');
    const ignoreUri = vscode.Uri.file(ignoreFilePath);

    let existing = '';
    try {
        const content = await vscode.workspace.fs.readFile(ignoreUri);
        existing = Buffer.from(content).toString('utf-8');
    } catch {
        // File does not exist yet; will be created
    }

    const relativePath = path.relative(workspaceRoot, filePath);
    const entry = `${relativePath}:${ruleId}`;

    if (existing.includes(entry)) {
        return;
    }

    const newContent = existing ? `${existing.trimEnd()}\n${entry}\n` : `${entry}\n`;
    await vscode.workspace.fs.writeFile(ignoreUri, Buffer.from(newContent, 'utf-8'));
}
