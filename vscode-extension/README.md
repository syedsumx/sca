# CodeScope - Static Code Analysis for VS Code

Real-time static code analysis, vulnerability detection, and auto-fix suggestions powered by the CodeScope SCA engine.

## Features

- **Real-time scanning** -- Automatically analyzes files on save and open.
- **Vulnerability detection** -- Identifies security vulnerabilities with CWE references.
- **Severity-based filtering** -- Configure minimum severity level (BLOCKER, CRITICAL, MAJOR, MINOR, INFO).
- **Auto-fix suggestions** -- Quick-fix code actions to resolve common issues.
- **Rule suppression** -- Inline ignore comments and file-level ignore support.
- **Status bar integration** -- Live issue count with color-coded severity indicators.
- **Workspace scanning** -- Analyze the entire workspace with a single command.

## Prerequisites

- Python 3.9 or later
- CodeScope SCA installed (`pip install codescope-sca` or installed from source)

## Installation

### From VSIX

1. Download the `.vsix` file from the releases page.
2. In VS Code, open the Command Palette and run **Extensions: Install from VSIX...**.
3. Select the downloaded file.

### From Marketplace

Search for "CodeScope" in the VS Code Extensions panel and click Install.

### From Source

```bash
cd vscode-extension
npm install
npm run compile
npx vsce package
code --install-extension codescope-vscode-0.1.0.vsix
```

## Commands

| Command | Description |
| --- | --- |
| `CodeScope: Analyze Current File` | Scan the active editor file |
| `CodeScope: Analyze Workspace` | Scan the entire workspace |
| `CodeScope: Show Rule Info` | Open documentation for a rule |
| `CodeScope: Apply Fix Suggestion` | Apply an auto-fix for a diagnostic |
| `CodeScope: Ignore Rule for This Line` | Insert an inline ignore comment |
| `CodeScope: Toggle Scan on Save` | Enable or disable automatic scanning on save |

## Configuration

| Setting | Type | Default | Description |
| --- | --- | --- | --- |
| `codescope.enable` | boolean | `true` | Enable or disable CodeScope |
| `codescope.scanOnSave` | boolean | `true` | Scan files on save |
| `codescope.scanOnOpen` | boolean | `true` | Scan files when opened |
| `codescope.severity.minimum` | string | `"MINOR"` | Minimum severity to display |
| `codescope.rules.disabled` | string[] | `[]` | Rule IDs to disable |
| `codescope.python.path` | string | `"python3"` | Python interpreter path |
| `codescope.autoFix.enabled` | boolean | `true` | Enable auto-fix code actions |
| `codescope.statusBar.enabled` | boolean | `true` | Show status bar item |

## Screenshots

*(Placeholder: editor showing inline diagnostics with wavy underlines)*

*(Placeholder: status bar showing issue count)*

*(Placeholder: code action menu with fix and ignore options)*

## License

MIT
