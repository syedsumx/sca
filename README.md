# CodeScope

A powerful Software Composition Analysis (SCA) tool for code quality and security analysis, similar to SonarQube.

## Features

- **Multi-language Support**: 12 languages supported
  - Python
  - JavaScript/TypeScript
  - Java
  - Go
  - C/C++
  - C#
  - Ruby
  - PHP
  - Rust
  - Swift
  - Kotlin
  - Scala
- **Security Vulnerability Detection**: SQL injection, command injection, XSS, XXE, deserialization, hardcoded secrets, and more
- **Code Smell Detection**: Long functions, deep nesting, high complexity, dead code
- **Code Metrics**: Cyclomatic complexity, cognitive complexity, lines of code, maintainability index
- **Technical Debt Calculation**: Estimates time to fix issues
- **Quality Gates**: Configurable pass/fail criteria for CI/CD
- **Multiple Output Formats**: Console, JSON, SARIF

## Installation

```bash
pip install codescope
```

For development:

```bash
pip install codescope[dev]
```

## Quick Start

### Scan a Project

```bash
# Scan current directory
codescope scan .

# Scan with specific output format
codescope scan . --format json

# Scan with SARIF output for GitHub/GitLab integration
codescope scan . --format sarif -o results.sarif

# Scan with quality gate
codescope scan . --quality-gate default
```

### Initialize Configuration

```bash
codescope init
```

This creates a `codescope.yml` configuration file.

### List Available Rules

```bash
codescope rules list
codescope rules list --language python
codescope rules show python:S3649  # SQL Injection rule
```

## Configuration

Create a `codescope.yml` file in your project root:

```yaml
project:
  key: "my-project"
  name: "My Project"

analysis:
  languages:
    - python
    - javascript
  sources:
    include:
      - "src/**"
    exclude:
      - "**/tests/**"
      - "**/node_modules/**"

rules:
  disabled:
    - "python:S100"  # Disable naming convention rule
  severities:
    "python:S3776": "MINOR"  # Override severity
  parameters:
    "python:S138":
      max_lines: 100

quality_gate:
  conditions:
    - metric: "new_bugs"
      operator: "GT"
      threshold: 0
    - metric: "new_vulnerabilities"
      operator: "GT"
      threshold: 0
```

## Supported Rules

### Python Security Rules

| Rule ID | Name | Description |
|---------|------|-------------|
| python:S3649 | SQL Injection | Detects potential SQL injection vulnerabilities |
| python:S2076 | Command Injection | Detects OS command injection |
| python:S5131 | XSS | Detects cross-site scripting vulnerabilities |
| python:S2068 | Hardcoded Credentials | Detects hardcoded passwords and secrets |
| python:S4790 | Weak Cryptography | Detects use of weak cryptographic algorithms |

### Python Code Smells

| Rule ID | Name | Description |
|---------|------|-------------|
| python:S138 | Function Too Long | Functions exceeding maximum line count |
| python:S3776 | Cognitive Complexity | Functions with high cognitive complexity |
| python:S1541 | Cyclomatic Complexity | Functions with high cyclomatic complexity |
| python:S134 | Deep Nesting | Code with excessive nesting depth |
| python:S1144 | Dead Code | Unused functions and variables |

## Output Formats

### Console (Default)

Beautiful, colored terminal output with rich formatting.

### JSON

Machine-readable JSON for integration with other tools.

### SARIF

Standard format for security tools, integrates with:
- GitHub Code Scanning
- GitLab Security Dashboard
- Azure DevOps
- VS Code SARIF Viewer

## CI/CD Integration

### GitHub Actions

```yaml
- name: Run CodeScope
  run: |
    pip install codescope
    codescope scan . --format sarif -o results.sarif --quality-gate default

- name: Upload SARIF
  uses: github/codeql-action/upload-sarif@v2
  with:
    sarif_file: results.sarif
```

### GitLab CI

```yaml
code_analysis:
  script:
    - pip install codescope
    - codescope scan . --format json -o codescope-report.json
  artifacts:
    reports:
      codequality: codescope-report.json
```

## Development

```bash
# Clone the repository
git clone https://github.com/codescope/codescope.git
cd codescope

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install in development mode
pip install -e ".[dev]"

# Run tests
pytest

# Run with coverage
pytest --cov=codescope --cov-report=html
```

## Architecture

```
codescope/
├── core/          # Foundation (models, config, enums)
├── parsers/       # Language parsers (tree-sitter)
├── analyzers/     # Analysis engines
├── rules/         # Detection rules
├── metrics/       # Code metrics calculation
├── quality_gates/ # Quality gate evaluation
├── reporters/     # Output formatters
└── cli/           # Command-line interface
```

## License

MIT License - see [LICENSE](LICENSE) for details.
