import * as vscode from 'vscode';

/**
 * Manages the CodeScope status bar item, showing issue counts and scan state.
 */
export class StatusBarManager {
    private statusBarItem: vscode.StatusBarItem;
    private isScanning: boolean = false;
    private errorCount: number = 0;
    private warningCount: number = 0;
    private infoCount: number = 0;
    private spinnerFrames: string[] = ['$(sync~spin)'];
    private disposed: boolean = false;

    constructor() {
        this.statusBarItem = vscode.window.createStatusBarItem(
            vscode.StatusBarAlignment.Left,
            100
        );
        this.statusBarItem.command = 'workbench.actions.view.problems';
        this.statusBarItem.tooltip = 'CodeScope - Click to open Problems panel';
        this.update();
    }

    /**
     * Show that a scan is in progress.
     */
    public showScanning(): void {
        this.isScanning = true;
        this.statusBarItem.text = `${this.spinnerFrames[0]} CodeScope`;
        this.statusBarItem.tooltip = 'CodeScope - Scanning...';
        this.statusBarItem.backgroundColor = undefined;
        this.statusBarItem.show();
    }

    /**
     * Update counts from the diagnostics collection.
     */
    public updateFromDiagnostics(diagnosticCollection: vscode.DiagnosticCollection): void {
        this.isScanning = false;
        this.errorCount = 0;
        this.warningCount = 0;
        this.infoCount = 0;

        diagnosticCollection.forEach((_uri: vscode.Uri, diagnostics: readonly vscode.Diagnostic[]) => {
            for (const diag of diagnostics) {
                switch (diag.severity) {
                    case vscode.DiagnosticSeverity.Error:
                        this.errorCount++;
                        break;
                    case vscode.DiagnosticSeverity.Warning:
                        this.warningCount++;
                        break;
                    case vscode.DiagnosticSeverity.Information:
                    case vscode.DiagnosticSeverity.Hint:
                        this.infoCount++;
                        break;
                }
            }
        });

        this.update();
    }

    /**
     * Show that the scan completed with an error.
     */
    public showError(message: string): void {
        this.isScanning = false;
        this.statusBarItem.text = '$(shield) CodeScope Error';
        this.statusBarItem.tooltip = `CodeScope - ${message}`;
        this.statusBarItem.backgroundColor = new vscode.ThemeColor(
            'statusBarItem.errorBackground'
        );
        this.statusBarItem.show();
    }

    /**
     * Set visibility based on configuration.
     */
    public setVisible(visible: boolean): void {
        if (visible) {
            this.statusBarItem.show();
        } else {
            this.statusBarItem.hide();
        }
    }

    /**
     * Dispose of the status bar item.
     */
    public dispose(): void {
        if (!this.disposed) {
            this.disposed = true;
            this.statusBarItem.dispose();
        }
    }

    private update(): void {
        if (this.disposed || this.isScanning) {
            return;
        }

        const total = this.errorCount + this.warningCount + this.infoCount;

        if (total === 0) {
            this.statusBarItem.text = '$(shield) 0 issues';
            this.statusBarItem.backgroundColor = undefined;
            this.statusBarItem.color = '#73c991'; // green
            this.statusBarItem.tooltip = 'CodeScope - No issues found';
        } else if (this.errorCount > 0) {
            this.statusBarItem.text = `$(shield) ${total} issues`;
            this.statusBarItem.backgroundColor = new vscode.ThemeColor(
                'statusBarItem.errorBackground'
            );
            this.statusBarItem.color = undefined;
            this.statusBarItem.tooltip = `CodeScope - ${this.errorCount} errors, ${this.warningCount} warnings, ${this.infoCount} info`;
        } else if (this.warningCount > 0) {
            this.statusBarItem.text = `$(shield) ${total} issues`;
            this.statusBarItem.backgroundColor = new vscode.ThemeColor(
                'statusBarItem.warningBackground'
            );
            this.statusBarItem.color = undefined;
            this.statusBarItem.tooltip = `CodeScope - ${this.warningCount} warnings, ${this.infoCount} info`;
        } else {
            this.statusBarItem.text = `$(shield) ${total} issues`;
            this.statusBarItem.backgroundColor = undefined;
            this.statusBarItem.color = undefined;
            this.statusBarItem.tooltip = `CodeScope - ${this.infoCount} informational issues`;
        }

        this.statusBarItem.show();
    }
}
