# CodeScope API Reference

Comprehensive API documentation with examples, error handling, and integration patterns.

> **OpenAPI Spec**: For machine-readable API schema, see `/api/openapi.json`
>
> **Interactive Docs**: Swagger UI at `/api/docs`, ReDoc at `/api/redoc`

## Table of Contents

- [Overview](#overview)
- [Authentication](#authentication)
- [Common Patterns](#common-patterns)
- [Endpoints](#endpoints)
  - [Analysis](#analysis)
  - [Projects](#projects)
  - [Issues](#issues)
  - [Rules](#rules)
  - [Dependencies](#dependencies)
  - [Coverage](#coverage)
  - [Trends](#trends)
  - [Export](#export)
  - [Custom Rules](#custom-rules)
  - [Webhooks](#webhooks)
- [Error Handling](#error-handling)
- [Rate Limiting](#rate-limiting)
- [Integration Examples](#integration-examples)

---

## Overview

### Base URL

```
Production: https://api.codescope.io/api/v1
Development: http://localhost:8000/api/v1
```

### Content Types

All requests and responses use JSON:

```
Content-Type: application/json
Accept: application/json
```

### Versioning

API version is included in the URL path (`/api/v1/`). Breaking changes will increment the version number.

---

## Authentication

CodeScope uses JWT (JSON Web Tokens) for authentication.

### Login

```http
POST /api/v1/auth/login
Content-Type: application/json

{
  "username": "user@example.com",
  "password": "your-password"
}
```

**Response:**

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 3600
}
```

### Using Tokens

Include the access token in the `Authorization` header:

```http
GET /api/v1/projects
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
```

### Refresh Token

```http
POST /api/v1/auth/refresh
Content-Type: application/json

{
  "refresh_token": "eyJhbGciOiJIUzI1NiIs..."
}
```

### API Keys

For CI/CD integrations, use API keys:

```http
GET /api/v1/projects
X-API-Key: cs_live_xxxxxxxxxxxxxxxxxxxx
```

Generate API keys via the dashboard or:

```http
POST /api/v1/auth/api-keys
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...

{
  "name": "CI Pipeline",
  "scopes": ["analysis:read", "analysis:write", "issues:read"]
}
```

---

## Common Patterns

### Pagination

List endpoints support pagination via query parameters:

```http
GET /api/v1/issues?page=1&page_size=50
```

**Response includes pagination metadata:**

```json
{
  "items": [...],
  "total": 1234,
  "page": 1,
  "page_size": 50,
  "pages": 25
}
```

### Filtering

Filter results using query parameters:

```http
GET /api/v1/issues?severity=BLOCKER,CRITICAL&type=VULNERABILITY&file_path=src/auth
```

**Common filter parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `severity` | string[] | Filter by severity levels |
| `type` | string[] | Filter by issue types |
| `file_path` | string | Filter by file path (partial match) |
| `rule_id` | string | Filter by rule ID |
| `tags` | string[] | Filter by tags |
| `created_after` | datetime | Filter by creation date |
| `created_before` | datetime | Filter by creation date |

### Sorting

Sort results using `sort_by` and `sort_order`:

```http
GET /api/v1/issues?sort_by=severity&sort_order=desc
```

### Field Selection

Request specific fields to reduce response size:

```http
GET /api/v1/issues?fields=id,rule_id,severity,message
```

---

## Endpoints

### Analysis

#### Trigger Analysis

Start a new code analysis.

```http
POST /api/v1/analysis
Authorization: Bearer <token>
Content-Type: application/json

{
  "project_id": "proj_123",
  "path": "/path/to/code",
  "config": {
    "rules": ["python:S3649", "python:S2076"],
    "exclude_patterns": ["**/tests/**", "**/migrations/**"]
  },
  "quality_gate": "default"
}
```

**Response:**

```json
{
  "analysis_id": "ana_456",
  "status": "PENDING",
  "created_at": "2025-01-30T10:00:00Z"
}
```

#### Get Analysis Status

```http
GET /api/v1/analysis/{analysis_id}
Authorization: Bearer <token>
```

**Response:**

```json
{
  "analysis_id": "ana_456",
  "project_id": "proj_123",
  "status": "COMPLETED",
  "started_at": "2025-01-30T10:00:00Z",
  "completed_at": "2025-01-30T10:05:23Z",
  "duration_seconds": 323,
  "issues_count": 42,
  "quality_gate": {
    "status": "PASSED",
    "conditions": [
      {"metric": "blocker_issues", "threshold": 0, "actual": 0, "passed": true},
      {"metric": "critical_issues", "threshold": 0, "actual": 0, "passed": true},
      {"metric": "coverage", "threshold": 80, "actual": 85.5, "passed": true}
    ]
  },
  "metrics": {
    "total_issues": 42,
    "by_severity": {"BLOCKER": 0, "CRITICAL": 0, "MAJOR": 15, "MINOR": 20, "INFO": 7},
    "by_type": {"BUG": 5, "VULNERABILITY": 3, "CODE_SMELL": 34},
    "lines_of_code": 15000,
    "coverage_percent": 85.5,
    "duplication_percent": 3.2
  }
}
```

#### List Analyses

```http
GET /api/v1/analysis?project_id=proj_123&status=COMPLETED&page=1&page_size=10
Authorization: Bearer <token>
```

#### Cancel Analysis

```http
POST /api/v1/analysis/{analysis_id}/cancel
Authorization: Bearer <token>
```

---

### Projects

#### List Projects

```http
GET /api/v1/projects
Authorization: Bearer <token>
```

**Response:**

```json
{
  "items": [
    {
      "id": "proj_123",
      "name": "codescope-api",
      "path": "/repos/codescope-api",
      "language": "python",
      "last_analysis": "2025-01-30T10:05:23Z",
      "quality_gate_status": "PASSED",
      "metrics": {
        "issues": 42,
        "vulnerabilities": 3,
        "coverage": 85.5
      }
    }
  ],
  "total": 5,
  "page": 1,
  "page_size": 20
}
```

#### Create Project

```http
POST /api/v1/projects
Authorization: Bearer <token>
Content-Type: application/json

{
  "name": "my-project",
  "path": "/path/to/project",
  "description": "My awesome project",
  "tags": ["backend", "python"]
}
```

#### Get Project

```http
GET /api/v1/projects/{project_id}
Authorization: Bearer <token>
```

#### Update Project

```http
PATCH /api/v1/projects/{project_id}
Authorization: Bearer <token>
Content-Type: application/json

{
  "description": "Updated description",
  "tags": ["backend", "python", "api"]
}
```

#### Delete Project

```http
DELETE /api/v1/projects/{project_id}
Authorization: Bearer <token>
```

#### Get Project Summary

```http
GET /api/v1/projects/{project_id}/summary
Authorization: Bearer <token>
```

**Response:**

```json
{
  "project_id": "proj_123",
  "name": "codescope-api",
  "quality_gate_status": "PASSED",
  "ratings": {
    "security": "B",
    "reliability": "A",
    "maintainability": "A"
  },
  "metrics": {
    "vulnerabilities": 5,
    "bugs": 12,
    "code_smells": 85,
    "coverage": 78.5,
    "duplications": 3.2,
    "lines_of_code": 15000
  },
  "trend": {
    "issues_delta": -5,
    "coverage_delta": 2.3
  }
}
```

---

### Issues

#### List Issues

```http
GET /api/v1/issues?project_id=proj_123&severity=BLOCKER,CRITICAL&page=1
Authorization: Bearer <token>
```

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_id` | string | Filter by project |
| `analysis_id` | string | Filter by analysis |
| `severity` | string[] | BLOCKER, CRITICAL, MAJOR, MINOR, INFO |
| `type` | string[] | BUG, VULNERABILITY, CODE_SMELL, SECURITY_HOTSPOT |
| `status` | string[] | OPEN, CONFIRMED, FALSE_POSITIVE, FIXED |
| `file_path` | string | File path filter (partial match) |
| `rule_id` | string | Filter by rule ID |
| `tags` | string[] | Filter by tags |
| `assignee` | string | Filter by assignee |
| `created_after` | datetime | Filter by date |
| `sort_by` | string | severity, created_at, file_path |
| `sort_order` | string | asc, desc |

**Response:**

```json
{
  "items": [
    {
      "id": "iss_789",
      "rule_id": "python:S3649",
      "rule_name": "SQL Injection",
      "severity": "BLOCKER",
      "type": "VULNERABILITY",
      "status": "OPEN",
      "message": "Use parameterized queries to prevent SQL injection",
      "location": {
        "file_path": "src/api/users.py",
        "start_line": 45,
        "end_line": 45,
        "start_column": 12,
        "end_column": 58
      },
      "snippet": "cursor.execute(f\"SELECT * FROM users WHERE id = {user_id}\")",
      "effort_minutes": 30,
      "cwe_ids": [89],
      "owasp_categories": ["A03:2021"],
      "tags": ["security", "sql", "injection"],
      "created_at": "2025-01-30T10:05:23Z"
    }
  ],
  "total": 42,
  "page": 1,
  "page_size": 20
}
```

#### Get Issue

```http
GET /api/v1/issues/{issue_id}
Authorization: Bearer <token>
```

#### Update Issue Status

```http
PATCH /api/v1/issues/{issue_id}
Authorization: Bearer <token>
Content-Type: application/json

{
  "status": "CONFIRMED",
  "assignee": "developer@example.com",
  "comment": "Confirmed, will fix in next sprint"
}
```

#### Bulk Update Issues

```http
PATCH /api/v1/issues/bulk
Authorization: Bearer <token>
Content-Type: application/json

{
  "issue_ids": ["iss_789", "iss_790", "iss_791"],
  "status": "FALSE_POSITIVE",
  "comment": "False positives - using ORM"
}
```

#### Get Issue Suggestions

```http
GET /api/v1/issues/{issue_id}/suggestions
Authorization: Bearer <token>
```

**Response:**

```json
{
  "issue_id": "iss_789",
  "suggestions": [
    {
      "type": "code_fix",
      "description": "Use parameterized query",
      "before": "cursor.execute(f\"SELECT * FROM users WHERE id = {user_id}\")",
      "after": "cursor.execute(\"SELECT * FROM users WHERE id = %s\", (user_id,))",
      "confidence": 0.95
    }
  ],
  "references": [
    {
      "title": "CWE-89: SQL Injection",
      "url": "https://cwe.mitre.org/data/definitions/89.html"
    },
    {
      "title": "OWASP SQL Injection Prevention",
      "url": "https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html"
    }
  ]
}
```

---

### Rules

#### List Rules

```http
GET /api/v1/rules?language=python&severity=BLOCKER,CRITICAL&type=VULNERABILITY
Authorization: Bearer <token>
```

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `language` | string | Filter by language |
| `severity` | string[] | Filter by severity |
| `type` | string[] | Filter by issue type |
| `tags` | string[] | Filter by tags |
| `enabled_only` | bool | Only return enabled rules |
| `search` | string | Search in name/description |

**Response:**

```json
{
  "items": [
    {
      "id": "python:S3649",
      "name": "SQL Injection",
      "description": "User-provided data should not be used directly in SQL queries",
      "severity": "BLOCKER",
      "type": "VULNERABILITY",
      "language": "python",
      "effort_minutes": 30,
      "cwe_ids": [89],
      "owasp_categories": ["A03:2021"],
      "tags": ["security", "sql", "injection", "owasp-top10"],
      "enabled": true,
      "parameters": {
        "checkStoredProcedures": true
      }
    }
  ],
  "total": 150
}
```

#### Get Rule

```http
GET /api/v1/rules/{rule_id}
Authorization: Bearer <token>
```

**Response includes full documentation:**

```json
{
  "id": "python:S3649",
  "name": "SQL Injection",
  "description": "User-provided data should not be used directly in SQL queries...",
  "severity": "BLOCKER",
  "type": "VULNERABILITY",
  "language": "python",
  "effort_minutes": 30,
  "cwe_ids": [89],
  "owasp_categories": ["A03:2021"],
  "tags": ["security", "sql", "injection"],
  "enabled": true,
  "parameters": {
    "checkStoredProcedures": {
      "type": "boolean",
      "default": true,
      "description": "Check stored procedure calls"
    }
  },
  "examples": {
    "noncompliant": "cursor.execute(f\"SELECT * FROM users WHERE id = {user_id}\")",
    "compliant": "cursor.execute(\"SELECT * FROM users WHERE id = %s\", (user_id,))"
  },
  "references": [
    {"title": "CWE-89", "url": "https://cwe.mitre.org/data/definitions/89.html"},
    {"title": "OWASP Injection", "url": "https://owasp.org/Top10/A03_2021-Injection/"}
  ]
}
```

#### Enable/Disable Rules

```http
POST /api/v1/rules/{rule_id}/enable
Authorization: Bearer <token>

POST /api/v1/rules/{rule_id}/disable
Authorization: Bearer <token>
```

#### Update Rule Parameters

```http
PATCH /api/v1/rules/{rule_id}/parameters
Authorization: Bearer <token>
Content-Type: application/json

{
  "max_lines": 100,
  "threshold": 15
}
```

---

### Dependencies

#### List Dependencies

```http
GET /api/v1/projects/{project_id}/dependencies
Authorization: Bearer <token>
```

**Response:**

```json
{
  "items": [
    {
      "name": "requests",
      "version": "2.28.0",
      "type": "runtime",
      "source": "pypi",
      "license": "Apache-2.0",
      "vulnerabilities": [
        {
          "id": "CVE-2023-32681",
          "severity": "MEDIUM",
          "title": "Unintended leak of Proxy-Authorization header",
          "fixed_in": "2.31.0"
        }
      ],
      "transitive": false,
      "dependents": ["my-project"]
    }
  ],
  "total": 45,
  "vulnerable_count": 3,
  "outdated_count": 12
}
```

#### Get Dependency Tree

```http
GET /api/v1/projects/{project_id}/dependencies/tree
Authorization: Bearer <token>
```

#### Check Vulnerabilities

```http
POST /api/v1/dependencies/check
Authorization: Bearer <token>
Content-Type: application/json

{
  "dependencies": [
    {"name": "requests", "version": "2.28.0"},
    {"name": "django", "version": "4.1.0"}
  ]
}
```

---

### Coverage

#### Get Coverage Report

```http
GET /api/v1/projects/{project_id}/coverage
Authorization: Bearer <token>
```

**Response:**

```json
{
  "project_id": "proj_123",
  "overall": {
    "line_coverage": 85.5,
    "branch_coverage": 72.3,
    "lines_covered": 12750,
    "lines_total": 14912,
    "branches_covered": 1850,
    "branches_total": 2560
  },
  "files": [
    {
      "path": "src/api/users.py",
      "line_coverage": 92.3,
      "branch_coverage": 85.0,
      "uncovered_lines": [45, 67, 89, 112]
    }
  ],
  "trend": {
    "previous": 83.2,
    "change": 2.3
  }
}
```

#### Upload Coverage

```http
POST /api/v1/projects/{project_id}/coverage
Authorization: Bearer <token>
Content-Type: multipart/form-data

file: coverage.xml
format: cobertura
```

---

### Trends

#### Get Metrics Trend

```http
GET /api/v1/projects/{project_id}/trends?metric=issues&period=30d
Authorization: Bearer <token>
```

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `metric` | string | issues, coverage, duplication, vulnerabilities |
| `period` | string | 7d, 30d, 90d, 1y |
| `granularity` | string | day, week, month |

**Response:**

```json
{
  "project_id": "proj_123",
  "metric": "issues",
  "period": "30d",
  "data": [
    {"date": "2025-01-01", "value": 55},
    {"date": "2025-01-02", "value": 52},
    {"date": "2025-01-03", "value": 48}
  ],
  "summary": {
    "start_value": 55,
    "end_value": 42,
    "change": -13,
    "change_percent": -23.6
  }
}
```

#### Get Quality History

```http
GET /api/v1/projects/{project_id}/trends/quality
Authorization: Bearer <token>
```

---

### Export

#### Export Issues

```http
GET /api/v1/export/issues?project_id=proj_123&format=sarif
Authorization: Bearer <token>
```

**Supported Formats:**

| Format | Description |
|--------|-------------|
| `json` | Raw JSON export |
| `sarif` | SARIF 2.1.0 (GitHub, GitLab compatible) |
| `csv` | CSV spreadsheet |
| `pdf` | PDF report |
| `html` | HTML report |
| `sonarqube` | SonarQube generic issue format |

#### Export SBOM

```http
GET /api/v1/projects/{project_id}/sbom?format=cyclonedx
Authorization: Bearer <token>
```

**Supported Formats:**

| Format | Description |
|--------|-------------|
| `cyclonedx` | CycloneDX 1.4 JSON |
| `spdx` | SPDX 2.3 JSON |

---

### Custom Rules

#### List Custom Rules

```http
GET /api/v1/custom-rules
Authorization: Bearer <token>
```

#### Create Custom Rule

```http
POST /api/v1/custom-rules
Authorization: Bearer <token>
Content-Type: application/json

{
  "id": "custom:NO_PRINT",
  "name": "No Print Statements",
  "description": "Print statements should not be used in production",
  "pattern": "\\bprint\\s*\\(",
  "message": "Replace print() with proper logging",
  "severity": "MINOR",
  "type": "CODE_SMELL",
  "languages": ["python"],
  "tags": ["logging"],
  "enabled": true
}
```

#### Update Custom Rule

```http
PUT /api/v1/custom-rules/{rule_id}
Authorization: Bearer <token>
Content-Type: application/json

{
  "severity": "MAJOR",
  "enabled": false
}
```

#### Delete Custom Rule

```http
DELETE /api/v1/custom-rules/{rule_id}
Authorization: Bearer <token>
```

#### Test Custom Rule

```http
POST /api/v1/custom-rules/test
Authorization: Bearer <token>
Content-Type: application/json

{
  "pattern": "\\bprint\\s*\\(",
  "test_code": "print('hello world')\nlogging.info('message')"
}
```

**Response:**

```json
{
  "matches": [
    {"line": 1, "column": 0, "text": "print('hello world')"}
  ],
  "match_count": 1
}
```

---

### Webhooks

#### List Webhooks

```http
GET /api/v1/webhooks
Authorization: Bearer <token>
```

#### Create Webhook

```http
POST /api/v1/webhooks
Authorization: Bearer <token>
Content-Type: application/json

{
  "url": "https://example.com/webhook",
  "events": ["analysis.completed", "quality_gate.failed"],
  "secret": "webhook-secret-key",
  "active": true
}
```

**Available Events:**

| Event | Description |
|-------|-------------|
| `analysis.started` | Analysis job started |
| `analysis.completed` | Analysis completed successfully |
| `analysis.failed` | Analysis failed |
| `quality_gate.passed` | Quality gate passed |
| `quality_gate.failed` | Quality gate failed |
| `issue.created` | New issue detected |
| `vulnerability.detected` | New vulnerability found |

#### Webhook Payload

```json
{
  "event": "analysis.completed",
  "timestamp": "2025-01-30T10:05:23Z",
  "data": {
    "analysis_id": "ana_456",
    "project_id": "proj_123",
    "project_name": "my-project",
    "status": "COMPLETED",
    "quality_gate": "PASSED",
    "issues_count": 42,
    "duration_seconds": 323
  }
}
```

#### Verify Webhook Signature

Webhooks include an `X-CodeScope-Signature` header:

```python
import hmac
import hashlib

def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)
```

---

## Error Handling

### Error Response Format

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid request parameters",
    "details": [
      {"field": "severity", "message": "Invalid severity value: EXTREME"}
    ],
    "request_id": "req_abc123"
  }
}
```

### Error Codes

| HTTP Status | Code | Description |
|-------------|------|-------------|
| 400 | `VALIDATION_ERROR` | Invalid request parameters |
| 401 | `UNAUTHORIZED` | Missing or invalid authentication |
| 403 | `FORBIDDEN` | Insufficient permissions |
| 404 | `NOT_FOUND` | Resource not found |
| 409 | `CONFLICT` | Resource conflict (e.g., duplicate) |
| 422 | `UNPROCESSABLE_ENTITY` | Semantic validation error |
| 429 | `RATE_LIMITED` | Too many requests |
| 500 | `INTERNAL_ERROR` | Server error |
| 503 | `SERVICE_UNAVAILABLE` | Service temporarily unavailable |

---

## Rate Limiting

API requests are rate-limited per API key/user:

| Tier | Requests/minute | Requests/hour |
|------|-----------------|---------------|
| Free | 60 | 1000 |
| Pro | 300 | 10000 |
| Enterprise | Unlimited | Unlimited |

Rate limit headers are included in responses:

```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 45
X-RateLimit-Reset: 1706612400
```

---

## Integration Examples

### GitHub Actions

```yaml
name: CodeScope Analysis
on: [push, pull_request]

jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run CodeScope
        env:
          CODESCOPE_API_KEY: ${{ secrets.CODESCOPE_API_KEY }}
        run: |
          curl -X POST "https://api.codescope.io/api/v1/analysis" \
            -H "X-API-Key: $CODESCOPE_API_KEY" \
            -H "Content-Type: application/json" \
            -d '{"path": ".", "quality_gate": "default"}'
```

### Python SDK

```python
from codescope import CodeScopeClient

client = CodeScopeClient(api_key="cs_live_xxx")

# Trigger analysis
analysis = client.analysis.create(
    project_id="proj_123",
    path="/path/to/code"
)

# Wait for completion
result = client.analysis.wait(analysis.id)

# Get issues
issues = client.issues.list(
    project_id="proj_123",
    severity=["BLOCKER", "CRITICAL"]
)

for issue in issues:
    print(f"{issue.rule_id}: {issue.message}")
```

### cURL Examples

```bash
# Login
curl -X POST "http://localhost:8000/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username": "user@example.com", "password": "password"}'

# List projects
curl -X GET "http://localhost:8000/api/v1/projects" \
  -H "Authorization: Bearer <token>"

# Trigger analysis
curl -X POST "http://localhost:8000/api/v1/analysis" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"project_id": "proj_123", "path": "/code"}'

# Export SARIF
curl -X GET "http://localhost:8000/api/v1/export/issues?project_id=proj_123&format=sarif" \
  -H "Authorization: Bearer <token>" \
  -o results.sarif
```

---

## SDKs and Libraries

Official SDKs:

- **Python**: `pip install codescope`
- **JavaScript/TypeScript**: `npm install @codescope/client`
- **Go**: `go get github.com/codescope/codescope-go`

Community SDKs:

- **Ruby**: `gem install codescope`
- **Java**: Maven Central `io.codescope:client`

---

## Additional Resources

- [OpenAPI Specification](/api/openapi.json)
- [Swagger UI](/api/docs)
- [ReDoc](/api/redoc)
- [Architecture Guide](ARCHITECTURE.md)
- [Developer Guide](DEVELOPER_GUIDE.md)
