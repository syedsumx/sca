import * as vscode from 'vscode';
import { Issue } from './analyzer';

export class CodeScopeCodeActionProvider implements vscode.CodeActionProvider {
    provideCodeActions(
        document: vscode.TextDocument,
        range: vscode.Range | vscode.Selection,
        context: vscode.CodeActionContext,
        token: vscode.CancellationToken
    ): vscode.ProviderResult<(vscode.CodeAction | vscode.Command)[]> {
        const codeActions: vscode.CodeAction[] = [];

        for (const diagnostic of context.diagnostics) {
            if (diagnostic.source !== 'CodeScope') {
                continue;
            }

            const issueData = (diagnostic as any).issueData as Issue | undefined;

            // Add "Learn more" action
            const learnMoreAction = new vscode.CodeAction(
                `Learn more about ${issueData?.rule_id || 'this issue'}`,
                vscode.CodeActionKind.QuickFix
            );
            learnMoreAction.command = {
                command: 'vscode.open',
                title: 'Learn more',
                arguments: [
                    vscode.Uri.parse(`https://codescope.dev/rules/${issueData?.rule_id || ''}`)
                ],
            };
            codeActions.push(learnMoreAction);

            // Add suggestion as quick fix if available
            if (issueData?.suggestion) {
                const suggestionAction = new vscode.CodeAction(
                    issueData.suggestion.substring(0, 60) + (issueData.suggestion.length > 60 ? '...' : ''),
                    vscode.CodeActionKind.QuickFix
                );
                suggestionAction.isPreferred = true;
                suggestionAction.diagnostics = [diagnostic];

                // Note: Actual code modification would require more context
                // For now, we just show the suggestion
                suggestionAction.command = {
                    command: 'editor.action.showHover',
                    title: 'Show suggestion',
                };

                codeActions.push(suggestionAction);
            }

            // Add "Disable rule" action
            const disableRuleAction = new vscode.CodeAction(
                `Disable rule ${issueData?.rule_id || ''}`,
                vscode.CodeActionKind.QuickFix
            );
            disableRuleAction.command = {
                command: 'workbench.action.openSettings',
                title: 'Open settings',
                arguments: [`codescope.rules.disabled`],
            };
            codeActions.push(disableRuleAction);

            // Add inline suppression comment
            if (issueData?.rule_id) {
                const suppressAction = new vscode.CodeAction(
                    `Suppress this issue with comment`,
                    vscode.CodeActionKind.QuickFix
                );
                suppressAction.edit = new vscode.WorkspaceEdit();

                const line = document.lineAt(diagnostic.range.start.line);
                const languageId = document.languageId;
                const suppressComment = this.getSuppressionComment(languageId, issueData.rule_id);

                suppressAction.edit.insert(
                    document.uri,
                    new vscode.Position(diagnostic.range.start.line, line.text.length),
                    suppressComment
                );

                codeActions.push(suppressAction);
            }
        }

        return codeActions;
    }

    private getSuppressionComment(languageId: string, ruleId: string): string {
        const comment = `codescope-disable-next-line ${ruleId}`;

        switch (languageId) {
            case 'python':
            case 'ruby':
            case 'yaml':
            case 'shell':
                return `  # ${comment}`;
            case 'javascript':
            case 'typescript':
            case 'java':
            case 'csharp':
            case 'go':
            case 'rust':
            case 'swift':
            case 'kotlin':
            case 'scala':
            case 'php':
                return `  // ${comment}`;
            default:
                return `  // ${comment}`;
        }
    }
}
