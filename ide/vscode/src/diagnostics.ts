import * as vscode from 'vscode';
import { Issue } from './analyzer';

export class IssueDiagnosticProvider {
    private diagnosticCollection: vscode.DiagnosticCollection | undefined;

    setDiagnosticCollection(collection: vscode.DiagnosticCollection) {
        this.diagnosticCollection = collection;
    }

    updateDiagnostics(uri: vscode.Uri, issues: Issue[]) {
        if (!this.diagnosticCollection) {
            return;
        }

        const diagnostics: vscode.Diagnostic[] = issues.map((issue) =>
            this.issueToDiagnostic(issue)
        );

        this.diagnosticCollection.set(uri, diagnostics);
    }

    clearDiagnostics(uri: vscode.Uri) {
        if (!this.diagnosticCollection) {
            return;
        }
        this.diagnosticCollection.delete(uri);
    }

    clearAll() {
        if (!this.diagnosticCollection) {
            return;
        }
        this.diagnosticCollection.clear();
    }

    private issueToDiagnostic(issue: Issue): vscode.Diagnostic {
        const range = new vscode.Range(
            Math.max(0, issue.start_line - 1),
            issue.start_column ? Math.max(0, issue.start_column - 1) : 0,
            Math.max(0, issue.end_line - 1),
            issue.end_column || 1000
        );

        const severity = this.getSeverity(issue.severity);

        const diagnostic = new vscode.Diagnostic(
            range,
            `${issue.message} [${issue.rule_id}]`,
            severity
        );

        diagnostic.source = 'CodeScope';
        diagnostic.code = {
            value: issue.rule_id,
            target: vscode.Uri.parse(`https://codescope.dev/rules/${issue.rule_id}`),
        };

        // Add tags based on issue type
        if (issue.issue_type === 'CODE_SMELL') {
            diagnostic.tags = [vscode.DiagnosticTag.Unnecessary];
        }

        // Store issue data for code actions
        (diagnostic as any).issueData = issue;

        return diagnostic;
    }

    private getSeverity(severity: Issue['severity']): vscode.DiagnosticSeverity {
        const config = vscode.workspace.getConfiguration('codescope');

        const severityMap: Record<string, vscode.DiagnosticSeverity> = {
            'Error': vscode.DiagnosticSeverity.Error,
            'Warning': vscode.DiagnosticSeverity.Warning,
            'Information': vscode.DiagnosticSeverity.Information,
            'Hint': vscode.DiagnosticSeverity.Hint,
        };

        const configKey = `severity.${severity.toLowerCase()}`;
        const configValue = config.get<string>(configKey);

        if (configValue && severityMap[configValue]) {
            return severityMap[configValue];
        }

        // Default mapping
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
                return vscode.DiagnosticSeverity.Warning;
        }
    }
}
