# CodeScope Testing Guide

This document provides comprehensive documentation for the testing infrastructure in CodeScope, including unit tests, integration tests, performance tests, and mutation testing.

## Table of Contents

1. [Testing Overview](#testing-overview)
2. [Running Tests](#running-tests)
3. [Test Categories](#test-categories)
4. [Backend Tests (Python)](#backend-tests-python)
5. [Frontend Tests (React)](#frontend-tests-react)
6. [Mutation Testing](#mutation-testing)
7. [Performance Testing](#performance-testing)
8. [Load Testing](#load-testing)
9. [Writing New Tests](#writing-new-tests)
10. [CI/CD Integration](#cicd-integration)

---

## Testing Overview

CodeScope uses a comprehensive testing strategy that includes:

| Test Type | Framework | Location | Purpose |
|-----------|-----------|----------|---------|
| Unit Tests | pytest | `tests/` | Test individual functions and classes |
| Integration Tests | pytest + TestClient | `tests/test_api_routes.py` | Test API endpoints |
| Performance Tests | pytest-benchmark | `tests/test_performance.py` | Benchmark critical paths |
| Edge Case Tests | pytest | `tests/test_edge_cases.py` | Test boundary conditions |
| Mutation Tests | mutmut | `src/codescope/` | Verify test quality |
| Frontend Tests | Jest + RTL | `dashboard/src/**/__tests__/` | Test React components |

---

## Running Tests

### Quick Start

```bash
# Run all backend tests
pytest

# Run with coverage
pytest --cov=src/codescope --cov-report=html

# Run specific test file
pytest tests/test_api_routes.py

# Run tests matching a pattern
pytest -k "test_auth"
```

### Backend Tests

```bash
# Run unit tests only
pytest -m "unit"

# Run integration tests only
pytest -m "integration"

# Run performance tests only
pytest -m "performance"

# Exclude slow tests
pytest -m "not slow"

# Run in parallel (uses pytest-xdist)
pytest -n auto

# Verbose output with full traceback
pytest -v --tb=long
```

### Frontend Tests

```bash
cd dashboard

# Run all frontend tests
npm test

# Run tests with coverage
npm run test:coverage

# Run tests in CI mode (no watch)
npm run test:ci

# Run specific test file
npm test -- --testPathPattern="IssuesByTypeChart"
```

---

## Test Categories

### Test Markers

Backend tests use pytest markers to categorize tests:

```python
@pytest.mark.unit
def test_parser_basic():
    """Quick unit test."""
    pass

@pytest.mark.integration
def test_api_endpoint():
    """Integration test requiring database."""
    pass

@pytest.mark.performance
def test_parse_large_file():
    """Performance benchmark test."""
    pass

@pytest.mark.slow
def test_full_analysis():
    """Slow test that may take several seconds."""
    pass
```

---

## Backend Tests (Python)

### Test Structure

```
tests/
├── conftest.py              # Shared fixtures
├── test_analysis.py         # Core analysis tests
├── test_api_routes.py       # API endpoint tests (837 lines)
├── test_edge_cases.py       # Edge case tests (875 lines)
├── test_features.py         # Feature tests (965 lines)
├── test_parsers.py          # Parser tests
├── test_performance.py      # Performance tests (665 lines)
├── test_quality_gates.py    # Quality gate tests
├── test_reporters.py        # Reporter tests
└── test_rules.py            # Rule engine tests
```

### Key Fixtures (conftest.py)

```python
@pytest.fixture
def sample_python_code():
    """Sample code with various issues."""
    return '''
    def vulnerable_function(user_input):
        os.system(user_input)  # Command injection
        eval(user_input)       # Code injection
    '''

@pytest.fixture
def clean_python_code():
    """Clean code without issues."""
    return '''
    def safe_function(x: int) -> int:
        return x * 2
    '''

@pytest.fixture
def temp_project(tmp_path):
    """Temporary project directory with sample files."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("x = 1")
    return tmp_path

@pytest.fixture
def client(app):
    """FastAPI test client."""
    return TestClient(app)

@pytest.fixture
def auth_headers():
    """Admin authentication headers."""
    token = TokenManager().create_access_token(
        user_id="test-user",
        email="test@example.com",
        role="admin",
    )
    return {"Authorization": f"Bearer {token}"}
```

### API Testing Examples

```python
class TestProjectsEndpoints:
    def test_list_projects(self, client, auth_headers):
        """Should list all projects."""
        response = client.get("/api/v1/projects", headers=auth_headers)
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_create_project_validation(self, client, auth_headers):
        """Should validate project creation payload."""
        response = client.post(
            "/api/v1/projects",
            headers=auth_headers,
            json={"invalid": "payload"},
        )
        assert response.status_code == 422
```

---

## Frontend Tests (React)

### Test Structure

```
dashboard/src/
├── setupTests.ts                          # Test configuration
├── components/
│   ├── charts/
│   │   └── __tests__/
│   │       ├── IssuesByTypeChart.test.tsx
│   │       ├── IssuesBySeverityChart.test.tsx
│   │       ├── TrendLineChart.test.tsx
│   │       ├── VulnerabilityTimeline.test.tsx
│   │       ├── HotspotHeatmap.test.tsx
│   │       └── ProjectComparison.test.tsx
│   └── common/
│       └── __tests__/
└── pages/
    └── __tests__/
```

### Component Testing Examples

```tsx
import { render, screen, fireEvent } from '@testing-library/react';
import IssuesByTypeChart from '../IssuesByTypeChart';

describe('IssuesByTypeChart', () => {
  it('renders without crashing', () => {
    const issues = [
      { id: '1', issue_type: 'BUG', severity: 'MAJOR', ... },
    ];
    render(<IssuesByTypeChart issues={issues} />);
    expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
  });

  it('displays empty state when no issues', () => {
    render(<IssuesByTypeChart issues={[]} />);
    expect(screen.getByText('No issues found')).toBeInTheDocument();
  });
});
```

### Mocking Recharts

Recharts components require DOM measurements. Mock them in tests:

```tsx
jest.mock('recharts', () => {
  const OriginalModule = jest.requireActual('recharts');
  return {
    ...OriginalModule,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="responsive-container">{children}</div>
    ),
  };
});
```

---

## Mutation Testing

Mutation testing verifies that your tests actually catch bugs by introducing small changes (mutations) to the code and checking if tests fail.

### Running Mutation Tests

```bash
# Run mutation testing on all code
mutmut run

# Run on specific module
mutmut run --paths-to-mutate=src/codescope/rules

# View results summary
mutmut results

# Show surviving mutants (tests that didn't catch changes)
mutmut show 1  # Show specific mutant

# Generate HTML report
mutmut html
open html/index.html
```

### Configuration (pyproject.toml)

```toml
[tool.mutmut]
paths_to_mutate = "src/codescope/"
tests_dir = "tests/"
runner = "python -m pytest -x --tb=no -q"
paths_to_exclude = [
    "src/codescope/cli/",
    "src/codescope/api/app.py",
]
```

### Interpreting Results

| Metric | Description | Target |
|--------|-------------|--------|
| Killed | Mutations caught by tests | High |
| Survived | Mutations not caught | Low |
| Timeout | Test took too long | Investigate |
| Suspicious | Unexpected behavior | Investigate |

**Mutation Score** = Killed / (Killed + Survived) × 100%

Target: **>80%** mutation score for critical modules like `rules/` and `analyzers/`.

### Common Mutation Types

- **Boundary mutations**: Change `<` to `<=`, `>` to `>=`
- **Arithmetic mutations**: Change `+` to `-`, `*` to `/`
- **Boolean mutations**: Change `True` to `False`, `and` to `or`
- **Return mutations**: Return `None` instead of value
- **Comparison mutations**: Swap comparison operators

---

## Performance Testing

Performance tests ensure critical operations meet latency requirements.

### Running Performance Tests

```bash
# Run performance tests
pytest -m performance -v

# Run with benchmark output
pytest tests/test_performance.py --benchmark-only

# Compare with baseline
pytest tests/test_performance.py --benchmark-compare
```

### Performance Test Examples

```python
class TestParserPerformance:
    @pytest.mark.performance
    def test_parse_small_file_under_10ms(self):
        """Small files should parse in under 10ms."""
        parser = PythonParser()
        code = create_large_python_file(100)  # ~100 lines

        _, elapsed = measure_time(parser.parse, code)
        assert elapsed < 0.01, f"Parse took {elapsed:.4f}s"

    @pytest.mark.performance
    def test_parse_large_file_under_1s(self):
        """Large files should parse in under 1 second."""
        parser = PythonParser()
        code = create_large_python_file(10000)  # ~10,000 lines

        _, elapsed = measure_time(parser.parse, code)
        assert elapsed < 1.0, f"Parse took {elapsed:.4f}s"
```

### Performance Benchmarks

| Operation | File Size | Target Latency |
|-----------|-----------|----------------|
| Parse file | 100 lines | < 10ms |
| Parse file | 1,000 lines | < 100ms |
| Parse file | 10,000 lines | < 1s |
| Analyze project | 10 files | < 5s |
| Analyze project | 50 files | < 30s |
| Security rule check | Per file | < 100ms |
| SBOM generation | 100 deps | < 1s |
| JSON report | 1,000 issues | < 1s |
| HTML report | 500 issues | < 2s |

---

## Load Testing

Load tests verify the system handles concurrent operations correctly.

### Concurrent Operations Tests

```python
class TestLoadHandling:
    @pytest.mark.performance
    def test_concurrent_file_parsing(self):
        """Parser should handle concurrent file parsing."""
        import concurrent.futures

        parser = PythonParser()
        codes = [create_large_python_file(200) for _ in range(20)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(parser.parse, code) for code in codes]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        assert len(results) == 20
```

### Memory Stress Tests

```python
class TestMemoryStress:
    @pytest.mark.performance
    def test_repeated_parsing_no_memory_leak(self):
        """Repeated parsing should not cause memory growth."""
        import tracemalloc

        parser = PythonParser()
        code = create_large_python_file(500)

        tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]

        for _ in range(50):
            parser.parse(code)
            gc.collect()

        final = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()

        growth = final - baseline
        assert growth < 10 * 1024 * 1024  # < 10MB growth
```

---

## Writing New Tests

### Test Naming Convention

```python
# Format: test_<what>_<expected_behavior>
def test_parser_handles_empty_file():
    pass

def test_api_returns_401_without_auth():
    pass

def test_rule_detects_sql_injection():
    pass
```

### Test Structure (AAA Pattern)

```python
def test_example():
    # Arrange - Set up test data
    parser = PythonParser()
    code = "x = 1"

    # Act - Execute the code under test
    result = parser.parse(code)

    # Assert - Verify the results
    assert result is not None
    assert len(result.errors) == 0
```

### Edge Cases Checklist

When writing tests, consider these edge cases:

- [ ] Empty input
- [ ] Single element/character
- [ ] Maximum allowed values
- [ ] Invalid/malformed input
- [ ] Unicode characters
- [ ] Very large input
- [ ] Concurrent access
- [ ] Network failures (for API tests)
- [ ] Permission denied scenarios
- [ ] Missing optional fields

---

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install -e ".[dev]"

      - name: Run tests with coverage
        run: pytest --cov=src/codescope --cov-report=xml

      - name: Upload coverage
        uses: codecov/codecov-action@v4
        with:
          files: coverage.xml

  mutation:
    runs-on: ubuntu-latest
    if: github.event_name == 'pull_request'
    steps:
      - uses: actions/checkout@v4

      - name: Run mutation tests on changed files
        run: |
          mutmut run --paths-to-mutate=src/codescope/rules
          mutmut results

  frontend:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: dashboard
    steps:
      - uses: actions/checkout@v4

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '20'

      - name: Install dependencies
        run: npm ci

      - name: Run tests
        run: npm run test:ci
```

### Coverage Requirements

| Component | Minimum Coverage |
|-----------|-----------------|
| Backend overall | 60% |
| Rules engine | 80% |
| Security analyzers | 85% |
| API routes | 70% |
| Frontend charts | 60% |

---

## Troubleshooting

### Common Issues

**Tests fail with import errors:**
```bash
# Ensure package is installed in development mode
pip install -e ".[dev]"
```

**Recharts tests fail:**
```bash
# Ensure ResizeObserver is polyfilled
npm install --save-dev resize-observer-polyfill
```

**Slow tests:**
```bash
# Skip slow tests during development
pytest -m "not slow"
```

**Mutation tests timeout:**
```bash
# Increase timeout or run on specific module
mutmut run --paths-to-mutate=src/codescope/rules --timeout=30
```

---

## Additional Resources

- [pytest documentation](https://docs.pytest.org/)
- [pytest-benchmark](https://pytest-benchmark.readthedocs.io/)
- [mutmut documentation](https://mutmut.readthedocs.io/)
- [React Testing Library](https://testing-library.com/docs/react-testing-library/intro/)
- [Jest documentation](https://jestjs.io/docs/getting-started)
