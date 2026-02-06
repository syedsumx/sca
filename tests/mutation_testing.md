# Mutation Testing Guide

This document describes how to run mutation testing for CodeScope using `mutmut`.

## Overview

Mutation testing helps identify weak spots in our test suite by making small changes (mutations) to the source code and checking if our tests catch them. If a mutation survives (tests still pass), it indicates a potential gap in test coverage.

## Installation

```bash
pip install mutmut
```

Or install all dev dependencies:
```bash
pip install -e ".[dev]"
```

## Running Mutation Tests

### Full Mutation Test Run

```bash
# Run mutation testing on the entire codebase
mutmut run

# This will take a while - mutations are tested one at a time
```

### Targeted Mutation Testing

```bash
# Test mutations only in specific module
mutmut run --paths-to-mutate src/codescope/parsers/

# Test mutations in a single file
mutmut run --paths-to-mutate src/codescope/core/analysis.py
```

### Viewing Results

```bash
# Show summary of results
mutmut results

# Show all surviving mutants
mutmut results --survivors

# Show details of a specific mutant
mutmut show <mutant_id>

# Generate HTML report
mutmut html
```

## Interpreting Results

- **Killed**: The mutation was caught by tests (good!)
- **Survived**: The mutation was NOT caught (indicates test gap)
- **Timeout**: The mutation caused tests to hang
- **Suspicious**: The mutation caused unexpected behavior

## Priority Modules for Mutation Testing

1. **Security Rules** (`src/codescope/rules/security/`) - Critical for security scanning
2. **Secret Scanner** (`src/codescope/analyzers/secrets/`) - Must catch all secret patterns
3. **Quality Gates** (`src/codescope/quality_gates/`) - Gate evaluation must be accurate
4. **Parsers** (`src/codescope/parsers/`) - Must correctly parse all code structures

## CI/CD Integration

Add to your CI pipeline:

```yaml
mutation-testing:
  script:
    - pip install mutmut
    - mutmut run --paths-to-mutate src/codescope/quality_gates/ --CI
    - mutmut results
  allow_failure: true  # Initially, until coverage improves
```

## Best Practices

1. **Start Small**: Begin with critical modules like security rules
2. **Address Survivors**: Each surviving mutant indicates a potential test gap
3. **Boundary Conditions**: Mutmut often reveals missing boundary checks
4. **Comparison Operators**: Pay attention to `<` vs `<=`, `==` vs `!=`
5. **Boolean Logic**: Ensure all branches are properly tested

## Example: Addressing a Surviving Mutant

If mutmut shows:
```
--- src/codescope/quality_gates/gate.py
+++ src/codescope/quality_gates/gate.py
@@ -45,7 +45,7 @@
-    if value >= threshold:
+    if value > threshold:
```

This indicates tests don't distinguish between `>=` and `>`. Add a test:

```python
def test_quality_gate_exactly_at_threshold():
    gate = QualityGate()
    gate.add_condition("coverage", ">=", 80)

    # Exactly at threshold should pass
    result = gate.evaluate({"coverage": 80.0})
    assert result.passed is True
```

## Mutation Score Target

- **Initial Target**: 60% mutation score
- **Long-term Target**: 80% mutation score for critical modules
- **Security Modules**: 90%+ mutation score

## Running Incremental Mutations

For faster feedback during development:

```bash
# Only run on files changed since last commit
mutmut run --paths-to-mutate $(git diff --name-only HEAD~1 | grep "\.py$" | xargs)
```
