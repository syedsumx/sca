# CodeScope Architecture

This document provides a comprehensive overview of CodeScope's architecture, design principles, and component interactions.

## Table of Contents

- [Overview](#overview)
- [System Architecture](#system-architecture)
- [Core Components](#core-components)
  - [Parsers](#parsers)
  - [Rules Engine](#rules-engine)
  - [Analyzers](#analyzers)
  - [API Layer](#api-layer)
  - [Dashboard](#dashboard)
- [Data Flow](#data-flow)
- [Data Models](#data-models)
- [Configuration System](#configuration-system)
- [Security Architecture](#security-architecture)
- [Extension Points](#extension-points)
- [Deployment](#deployment)

---

## Overview

CodeScope is a comprehensive Software Composition Analysis (SCA) tool that combines static analysis, dependency scanning, and security vulnerability detection. Built with Python and React, it provides:

- **Multi-language support** via tree-sitter parsing
- **Extensible rule engine** with pattern and AST-based detection
- **REST API** for CI/CD integration
- **Interactive dashboard** for visualization
- **Compliance reporting** (CWE, OWASP, SARIF)

### Technology Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.11+, FastAPI, Pydantic |
| Parsing | tree-sitter (multi-language AST) |
| Database | SQLite/PostgreSQL (configurable) |
| Frontend | React 18, TypeScript, Tailwind CSS |
| Charts | Recharts, Chart.js |
| CLI | Typer |
| Testing | pytest, Jest, React Testing Library |

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CLIENT LAYER                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │   CLI        │  │  Dashboard   │  │  IDE Plugin  │  │  CI/CD       │    │
│  │  (Typer)     │  │  (React)     │  │  (LSP)       │  │  Webhooks    │    │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘    │
└─────────┼─────────────────┼─────────────────┼─────────────────┼────────────┘
          │                 │                 │                 │
          ▼                 ▼                 ▼                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              API LAYER                                       │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                     FastAPI Application                              │    │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐   │    │
│  │  │  Auth   │  │ Analysis│  │  Rules  │  │ Projects│  │  Export │   │    │
│  │  │ Routes  │  │ Routes  │  │ Routes  │  │ Routes  │  │ Routes  │   │    │
│  │  └─────────┘  └─────────┘  └─────────┘  └─────────┘  └─────────┘   │    │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐   │    │
│  │  │  Issues │  │  Trends │  │Coverage │  │  SBOM   │  │ Webhooks│   │    │
│  │  │ Routes  │  │ Routes  │  │ Routes  │  │ Routes  │  │ Routes  │   │    │
│  │  └─────────┘  └─────────┘  └─────────┘  └─────────┘  └─────────┘   │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           ORCHESTRATION LAYER                                │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                    Analysis Orchestrator                             │    │
│  │  • Coordinates analysis workflow                                     │    │
│  │  • Manages parallel execution                                        │    │
│  │  • Aggregates results                                                │    │
│  │  • Applies quality gates                                             │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     │
          ┌──────────────────────────┼──────────────────────────┐
          ▼                          ▼                          ▼
┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐
│   PARSING LAYER     │  │   RULES ENGINE      │  │    ANALYZERS        │
├─────────────────────┤  ├─────────────────────┤  ├─────────────────────┤
│ ┌─────────────────┐ │  │ ┌─────────────────┐ │  │ ┌─────────────────┐ │
│ │ Parser Registry │ │  │ │  Rule Registry  │ │  │ │  Dependencies   │ │
│ └────────┬────────┘ │  │ └────────┬────────┘ │  │ └─────────────────┘ │
│          │          │  │          │          │  │ ┌─────────────────┐ │
│ ┌────────▼────────┐ │  │ ┌────────▼────────┐ │  │ │   Coverage      │ │
│ │  Python Parser  │ │  │ │  Pattern Rules  │ │  │ └─────────────────┘ │
│ │  JS/TS Parser   │ │  │ │   AST Rules     │ │  │ ┌─────────────────┐ │
│ │  Java Parser    │ │  │ │  Custom Rules   │ │  │ │    Secrets      │ │
│ │  Go Parser      │ │  │ └─────────────────┘ │  │ └─────────────────┘ │
│ │  ... (10 more)  │ │  │                     │  │ ┌─────────────────┐ │
│ └─────────────────┘ │  │                     │  │ │   Duplication   │ │
│                     │  │                     │  │ └─────────────────┘ │
│ ┌─────────────────┐ │  │                     │  │ ┌─────────────────┐ │
│ │   tree-sitter   │ │  │                     │  │ │    IaC Scan     │ │
│ │    (AST Gen)    │ │  │                     │  │ └─────────────────┘ │
│ └─────────────────┘ │  │                     │  │ ┌─────────────────┐ │
└─────────────────────┘  └─────────────────────┘  │ │  AI Vetting     │ │
                                                  │ └─────────────────┘ │
                                                  └─────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            DATA LAYER                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │   Projects   │  │    Issues    │  │   Metrics    │  │   Trends     │    │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘    │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │ Dependencies │  │  Suppressions│  │   Coverage   │  │   Webhooks   │    │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Core Components

### Parsers

The parsing layer transforms source code into Abstract Syntax Trees (ASTs) for analysis.

```
src/codescope/parsers/
├── base.py           # BaseParser abstract class
├── models.py         # ASTNode, FunctionDef, Import, Symbol
├── registry.py       # ParserRegistry for discovery
├── python/           # Python parser (tree-sitter-python)
├── javascript/       # JavaScript/TypeScript parser
├── java/             # Java parser
├── go/               # Go parser
├── cpp/              # C/C++ parser
├── csharp/           # C# parser
├── php/              # PHP parser
├── ruby/             # Ruby parser
├── rust/             # Rust parser
├── kotlin/           # Kotlin parser
├── scala/            # Scala parser
└── swift/            # Swift parser
```

**Parser Interface:**

```python
class BaseParser(ABC):
    """Abstract base for language parsers."""

    language: str
    file_extensions: list[str]

    @abstractmethod
    def parse(self, source: str, file_path: str) -> ParsedFile:
        """Parse source code into structured representation."""

    @abstractmethod
    def extract_functions(self, tree) -> list[FunctionDef]:
        """Extract function definitions."""

    @abstractmethod
    def extract_imports(self, tree) -> list[Import]:
        """Extract import statements."""
```

**Parsed File Structure:**

```python
@dataclass
class ParsedFile:
    path: Path
    language: str
    content: str
    lines: list[str]
    ast: ASTNode
    functions: list[FunctionDef]
    classes: list[ClassDef]
    imports: list[Import]
    metrics: FileMetrics
```

### Rules Engine

The rules engine provides the detection logic with three rule types:

```
src/codescope/rules/
├── base.py           # Rule, PatternRule, ASTRule base classes
├── registry.py       # RuleRegistry - auto-discovery
├── custom/
│   └── engine.py     # YAML-based custom rules
├── python/
│   ├── security.py   # Vulnerability detection
│   ├── smells.py     # Code smell detection
│   └── bugs.py       # Bug pattern detection
├── javascript/
├── java/
├── go/
├── csharp/
├── php/
└── ruby/
```

**Rule Types:**

| Type | Use Case | Performance |
|------|----------|-------------|
| `PatternRule` | Regex-based detection | Fast (line-by-line) |
| `ASTRule` | Semantic analysis | Medium (tree traversal) |
| `CustomRule` | User-defined YAML | Fast (regex) |

**Rule Registration:**

```python
# Decorator-based auto-registration
@RuleRegistry.register
class SQLInjectionRule(Rule):
    id = "python:S3649"
    # ...

# Registry access
registry = RuleRegistry()
all_rules = registry.get_all_rules()           # dict[str, Rule]
python_rules = registry.get_rules_for_language("python")
security_rules = registry.get_rules_by_tag("security")
```

### Analyzers

Domain-specific analysis engines:

```
src/codescope/analyzers/
├── orchestrator.py       # Master orchestration
├── aivetting/            # AI-powered issue analysis
├── coverage/             # Code coverage integration
├── dataflow/             # Data flow analysis
├── deadcode/             # Dead code detection
├── dependencies/         # Dependency scanning
│   ├── scanner.py        # Core scanner
│   ├── npm.py            # package.json, package-lock.json
│   ├── pip.py            # requirements.txt, Pipfile
│   ├── maven.py          # pom.xml
│   ├── gradle.py         # build.gradle
│   └── vulnerability.py  # CVE database lookup
├── duplication.py        # Code clone detection
├── license/              # License compliance
├── secrets/              # Secrets detection
└── complexity/           # Complexity metrics
```

**Analyzer Interface:**

```python
class BaseAnalyzer(ABC):
    """Abstract base for analyzers."""

    name: str
    description: str

    @abstractmethod
    def analyze(self, project_path: Path, config: AnalysisConfig) -> AnalysisResult:
        """Run analysis on project."""
```

### API Layer

REST API built with FastAPI:

```
src/codescope/api/
├── app.py                # Application factory
├── models/
│   └── schemas.py        # Pydantic request/response models
└── routes/
    ├── analysis.py       # Trigger/manage scans
    ├── projects.py       # Project CRUD
    ├── issues.py         # Issue queries
    ├── rules.py          # Rule management
    ├── dependencies.py   # Dependency data
    ├── coverage.py       # Coverage data
    ├── trends.py         # Historical metrics
    ├── export.py         # Multi-format export
    ├── custom_rules.py   # YAML rule CRUD
    ├── remediation.py    # Fix suggestions
    ├── autofix.py        # Auto-remediation
    ├── secrets.py        # Secrets management
    ├── sbom.py           # SBOM generation
    ├── sarif_import.py   # SARIF import
    ├── iac.py            # IaC scanning
    ├── container.py      # Container scanning
    ├── compare.py        # Branch comparison
    ├── teams.py          # Team management
    ├── webhooks.py       # Event webhooks
    ├── compliance.py     # Compliance reports
    └── auth.py           # Authentication
```

**API Organization:**

| Route Group | Purpose | Auth Level |
|-------------|---------|------------|
| `/auth` | Login, register, tokens | Public |
| `/projects` | Project management | Viewer+ |
| `/analysis` | Trigger scans | Analyst+ |
| `/issues` | Issue queries | Viewer+ |
| `/rules` | Rule catalog | Viewer+ |
| `/export` | Data export | Viewer+ |
| `/custom-rules` | Custom rule CRUD | Analyst+ |
| `/webhooks` | Event subscriptions | Admin |

### Dashboard

React-based web interface:

```
dashboard/src/
├── components/
│   ├── charts/           # Visualization components
│   │   ├── IssuesByTypeChart.tsx
│   │   ├── IssuesBySeverityChart.tsx
│   │   ├── TrendLineChart.tsx
│   │   ├── VulnerabilityTimeline.tsx
│   │   ├── HotspotHeatmap.tsx
│   │   └── ProjectComparison.tsx
│   ├── common/           # Reusable UI components
│   └── layout/           # Layout components
├── pages/
│   ├── Dashboard/        # Main dashboard
│   ├── Projects/         # Project list/detail
│   ├── Issues/           # Issue browser
│   ├── Rules/            # Rule catalog
│   ├── Trends/           # Historical trends
│   ├── Compare/          # Branch/project comparison
│   └── Settings/         # Configuration
├── services/             # API client
├── hooks/                # Custom React hooks
├── types/                # TypeScript definitions
└── utils/                # Utility functions
```

---

## Data Flow

### Analysis Workflow

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Source    │────▶│   Parsers   │────▶│  ParsedFile │
│   Code      │     │ (tree-sitter)│    │  (AST+Meta) │
└─────────────┘     └─────────────┘     └──────┬──────┘
                                               │
          ┌────────────────────────────────────┼────────────────────────────────┐
          │                                    │                                │
          ▼                                    ▼                                ▼
┌─────────────────┐              ┌─────────────────────┐              ┌─────────────────┐
│  Rules Engine   │              │     Analyzers       │              │    Metrics      │
│ (Pattern+AST)   │              │ (Deps, Coverage...) │              │  Calculator     │
└────────┬────────┘              └──────────┬──────────┘              └────────┬────────┘
         │                                  │                                  │
         │    ┌─────────────────────────────┼──────────────────────────────────┘
         │    │                             │
         ▼    ▼                             ▼
    ┌──────────────────────────────────────────────────────────┐
    │                    Orchestrator                           │
    │  • Aggregates Issues                                      │
    │  • Calculates Quality Gate                                │
    │  • Generates Reports                                      │
    └────────────────────────────┬─────────────────────────────┘
                                 │
          ┌──────────────────────┼──────────────────────┐
          ▼                      ▼                      ▼
    ┌───────────┐         ┌───────────┐         ┌───────────┐
    │  Storage  │         │  Reports  │         │  Webhooks │
    │ (Database)│         │(JSON/SARIF)│         │  (Events) │
    └───────────┘         └───────────┘         └───────────┘
```

### Request Flow

```
Client Request
      │
      ▼
┌─────────────────┐
│   API Gateway   │ ◀── Rate Limiting, CORS
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Authentication │ ◀── JWT Token Validation
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Authorization  │ ◀── Role-based Access Control
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Route Handler  │ ◀── Business Logic
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   Data Layer    │ ◀── Database/Cache
└────────┬────────┘
         │
         ▼
    Response
```

---

## Data Models

### Core Enums

```python
class Severity(str, Enum):
    BLOCKER = "BLOCKER"     # Critical security/stability issues
    CRITICAL = "CRITICAL"   # Severe issues requiring immediate attention
    MAJOR = "MAJOR"         # Significant issues
    MINOR = "MINOR"         # Minor issues
    INFO = "INFO"           # Informational findings

class IssueType(str, Enum):
    BUG = "BUG"                         # Functional bugs
    VULNERABILITY = "VULNERABILITY"     # Security vulnerabilities
    CODE_SMELL = "CODE_SMELL"           # Maintainability issues
    SECURITY_HOTSPOT = "SECURITY_HOTSPOT"  # Security-sensitive code

class AnalysisStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class QualityGateStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    WARN = "WARN"
    NONE = "NONE"
```

### Core Models

```python
@dataclass
class Location:
    file_path: Path
    start_line: int
    end_line: int
    start_column: int = 0
    end_column: int = 0
    snippet: str = ""

@dataclass
class Issue:
    rule_id: str
    rule_name: str
    message: str
    location: Location
    severity: Severity
    issue_type: IssueType
    id: UUID
    effort_minutes: int
    cwe_ids: list[int]
    owasp_categories: list[str]
    tags: list[str]
    attributes: dict[str, Any]

@dataclass
class FileMetrics:
    lines_of_code: int
    logical_lines: int
    comment_lines: int
    blank_lines: int
    cyclomatic_complexity: int
    cognitive_complexity: int
    maintainability_index: float
    function_count: int
    class_count: int

@dataclass
class AnalysisResult:
    project_name: str
    timestamp: datetime
    duration_seconds: float
    issues: list[Issue]
    metrics: ProjectMetrics
    quality_gate: QualityGateResult
    dependencies: list[Dependency]
    coverage: CoverageReport
```

---

## Configuration System

### Configuration Hierarchy

```
1. Default values (code)
       ▼
2. System config (/etc/codescope/config.yml)
       ▼
3. User config (~/.codescope/config.yml)
       ▼
4. Project config (./codescope.yml)
       ▼
5. Environment variables (CODESCOPE_*)
       ▼
6. CLI arguments (--option)
       ▼
7. API request parameters
```

### Configuration File

```yaml
# codescope.yml
project:
  name: "my-project"
  version: "1.0.0"

analysis:
  include:
    - "src/**/*.py"
    - "lib/**/*.py"
  exclude:
    - "**/tests/**"
    - "**/migrations/**"
    - "**/__pycache__/**"

rules:
  disabled:
    - "python:S100"       # Disable specific rules
  severities:
    "python:S138": "MINOR"  # Override severity
  parameters:
    "python:S138":
      max_lines: 100      # Override parameters
    "python:S1541":
      threshold: 15

custom_rules:
  - custom_rules/*.yaml   # Load custom rules

quality_gates:
  default:
    blocker: 0            # Max blocker issues
    critical: 0           # Max critical issues
    major: 10             # Max major issues
    coverage: 80          # Min coverage %
    duplication: 5        # Max duplication %

dependencies:
  vulnerability_threshold: "HIGH"
  license_policy: "permissive"

notifications:
  slack:
    webhook_url: "${SLACK_WEBHOOK_URL}"
    on_failure: true
  email:
    recipients:
      - "team@example.com"
```

---

## Security Architecture

### Authentication

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Client    │────▶│  /api/auth  │────▶│  JWT Token  │
│  (Login)    │     │   /login    │     │  Generation │
└─────────────┘     └─────────────┘     └──────┬──────┘
                                               │
                                               ▼
                                        ┌─────────────┐
                                        │ Access Token│
                                        │ + Refresh   │
                                        └─────────────┘
```

### Authorization (RBAC)

| Role | Permissions |
|------|-------------|
| `VIEWER` | Read projects, issues, rules, trends |
| `ANALYST` | Trigger scans, manage custom rules, export |
| `ADMIN` | User management, webhooks, system config |

### Security Measures

1. **Input Validation**: Pydantic models validate all inputs
2. **SQL Injection Prevention**: Parameterized queries via ORM
3. **XSS Prevention**: Output encoding, CSP headers
4. **CSRF Protection**: Token-based requests
5. **Rate Limiting**: Configurable per-endpoint limits
6. **Secrets Management**: Environment variable injection
7. **Audit Logging**: Security event tracking

---

## Extension Points

CodeScope provides multiple extension mechanisms:

### 1. Custom Rules (YAML)

```yaml
rules:
  - id: "custom:MY_RULE"
    pattern: "..."
    # See DEVELOPER_GUIDE.md
```

### 2. Rule Classes (Python)

```python
@RuleRegistry.register
class MyRule(Rule):
    id = "lang:SXXXX"
    # See DEVELOPER_GUIDE.md
```

### 3. Parser Plugins

```python
@ParserRegistry.register("mylang")
class MyLangParser(BaseParser):
    # See DEVELOPER_GUIDE.md
```

### 4. Analyzer Modules

```python
class MyAnalyzer(BaseAnalyzer):
    # See DEVELOPER_GUIDE.md
```

### 5. API Routes

```python
@router.get("/my-endpoint")
async def my_endpoint():
    # See DEVELOPER_GUIDE.md
```

### 6. Webhook Events

```python
# Subscribe to events
webhooks.on("analysis.completed", handler)
```

---

## Deployment

### Docker

```dockerfile
# Dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install -e .
EXPOSE 8000
CMD ["uvicorn", "codescope.api.app:app", "--host", "0.0.0.0"]
```

```yaml
# docker-compose.yml
services:
  api:
    build: .
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql://...
      - CODESCOPE_SECRET_KEY=${SECRET_KEY}

  dashboard:
    build: ./dashboard
    ports:
      - "3000:80"

  db:
    image: postgres:15
    volumes:
      - pgdata:/var/lib/postgresql/data
```

### Kubernetes

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: codescope-api
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: api
          image: codescope:latest
          ports:
            - containerPort: 8000
          envFrom:
            - secretRef:
                name: codescope-secrets
```

### CI/CD Integration

```yaml
# GitHub Actions
- name: Run CodeScope
  uses: codescope/action@v1
  with:
    path: ./src
    quality-gate: true
    sarif-output: results.sarif

# GitLab CI
codescope:
  script:
    - codescope scan --format sarif -o gl-sast-report.json
  artifacts:
    reports:
      sast: gl-sast-report.json
```

---

## Performance Considerations

### Scalability

- **Parallel parsing**: Multi-threaded file processing
- **Incremental analysis**: Cache unchanged file results
- **Worker pools**: Distribute analysis across machines
- **Database indexing**: Optimized query patterns

### Resource Limits

```yaml
# Resource configuration
analysis:
  max_file_size: 10MB
  max_files: 10000
  timeout_seconds: 3600
  max_memory: 4GB
```

### Caching

- **Parse cache**: Reuse AST for unchanged files
- **Rule cache**: Cache regex compilation
- **Result cache**: Store analysis results
- **API cache**: HTTP response caching

---

## Additional Resources

- [Developer Guide](DEVELOPER_GUIDE.md) - Extending CodeScope
- [API Reference](API_REFERENCE.md) - Complete API documentation
- [Rule Catalog](RULES.md) - All available rules
- [Testing Guide](TESTING.md) - Testing strategies
