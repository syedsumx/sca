import * as vscode from 'vscode';
import * as path from 'path';
import { Issue } from './analyzer';

export class IssueTreeProvider implements vscode.TreeDataProvider<IssueTreeItem> {
    private _onDidChangeTreeData: vscode.EventEmitter<IssueTreeItem | undefined | null | void> =
        new vscode.EventEmitter<IssueTreeItem | undefined | null | void>();
    readonly onDidChangeTreeData: vscode.Event<IssueTreeItem | undefined | null | void> =
        this._onDidChangeTreeData.event;

    private issuesByFile: Map<string, Issue[]> = new Map();

    updateIssues(filePath: string, issues: Issue[]) {
        if (issues.length > 0) {
            this.issuesByFile.set(filePath, issues);
        } else {
            this.issuesByFile.delete(filePath);
        }
        this._onDidChangeTreeData.fire();
    }

    clear() {
        this.issuesByFile.clear();
        this._onDidChangeTreeData.fire();
    }

    getTreeItem(element: IssueTreeItem): vscode.TreeItem {
        return element;
    }

    getChildren(element?: IssueTreeItem): Thenable<IssueTreeItem[]> {
        if (!element) {
            // Root level - show files with issues
            const items: IssueTreeItem[] = [];

            for (const [filePath, issues] of this.issuesByFile) {
                const item = new IssueTreeItem(
                    path.basename(filePath),
                    vscode.TreeItemCollapsibleState.Collapsed,
                    'file',
                    filePath,
                    issues
                );

                // Count by severity
                const blockers = issues.filter((i) => i.severity === 'BLOCKER').length;
                const criticals = issues.filter((i) => i.severity === 'CRITICAL').length;
                const majors = issues.filter((i) => i.severity === 'MAJOR').length;

                item.description = `${issues.length} issues`;
                if (blockers > 0) {
                    item.description += ` (${blockers} blockers)`;
                } else if (criticals > 0) {
                    item.description += ` (${criticals} critical)`;
                }

                items.push(item);
            }

            // Sort by issue count (most issues first)
            items.sort((a, b) => (b.issues?.length || 0) - (a.issues?.length || 0));

            return Promise.resolve(items);
        } else if (element.type === 'file') {
            // Show issues for a file
            const issues = element.issues || [];
            const items: IssueTreeItem[] = issues.map((issue) => {
                const item = new IssueTreeItem(
                    issue.message.substring(0, 60) + (issue.message.length > 60 ? '...' : ''),
                    vscode.TreeItemCollapsibleState.None,
                    'issue',
                    element.filePath,
                    undefined,
                    issue
                );

                item.description = `Line ${issue.start_line}`;

                // Set icon based on severity
                item.iconPath = this.getSeverityIcon(issue.severity);

                // Set command to navigate to issue
                item.command = {
                    command: 'vscode.open',
                    title: 'Go to issue',
                    arguments: [
                        vscode.Uri.file(element.filePath!),
                        {
                            selection: new vscode.Range(
                                issue.start_line - 1,
                                0,
                                issue.start_line - 1,
                                0
                            ),
                        },
                    ],
                };

                return item;
            });

            // Sort by severity (most severe first)
            const severityOrder: Record<string, number> = {
                BLOCKER: 0,
                CRITICAL: 1,
                MAJOR: 2,
                MINOR: 3,
                INFO: 4,
            };
            items.sort((a, b) => {
                const aOrder = severityOrder[a.issue?.severity || 'INFO'] || 4;
                const bOrder = severityOrder[b.issue?.severity || 'INFO'] || 4;
                return aOrder - bOrder;
            });

            return Promise.resolve(items);
        }

        return Promise.resolve([]);
    }

    private getSeverityIcon(severity: Issue['severity']): vscode.ThemeIcon {
        switch (severity) {
            case 'BLOCKER':
                return new vscode.ThemeIcon('error', new vscode.ThemeColor('errorForeground'));
            case 'CRITICAL':
                return new vscode.ThemeIcon('warning', new vscode.ThemeColor('errorForeground'));
            case 'MAJOR':
                return new vscode.ThemeIcon('warning', new vscode.ThemeColor('editorWarning.foreground'));
            case 'MINOR':
                return new vscode.ThemeIcon('info', new vscode.ThemeColor('editorInfo.foreground'));
            case 'INFO':
                return new vscode.ThemeIcon('lightbulb', new vscode.ThemeColor('editorHint.foreground'));
            default:
                return new vscode.ThemeIcon('circle-outline');
        }
    }
}

class IssueTreeItem extends vscode.TreeItem {
    constructor(
        public readonly label: string,
        public readonly collapsibleState: vscode.TreeItemCollapsibleState,
        public readonly type: 'file' | 'issue',
        public readonly filePath?: string,
        public readonly issues?: Issue[],
        public readonly issue?: Issue
    ) {
        super(label, collapsibleState);

        if (type === 'file') {
            this.contextValue = 'file';
            this.iconPath = vscode.ThemeIcon.File;
            this.tooltip = filePath;
        } else {
            this.contextValue = 'issue';
            this.tooltip = issue?.message;
        }
    }
}
