"""Quality gate evaluation."""

from dataclasses import dataclass, field
from typing import Any

from codescope.core.config import QualityGateConfig, QualityGateCondition
from codescope.core.enums import QualityGateStatus
from codescope.core.models import AnalysisResults, QualityGateResult


@dataclass
class ConditionResult:
    """Result of evaluating a single condition."""

    condition: QualityGateCondition
    actual_value: Any
    passed: bool
    message: str = ""


class QualityGate:
    """Evaluates quality gate conditions against analysis results."""

    # Default quality gate conditions
    DEFAULT_CONDITIONS = [
        QualityGateCondition(metric="bugs", operator="GT", threshold=0),
        QualityGateCondition(metric="vulnerabilities", operator="GT", threshold=0),
        QualityGateCondition(metric="code_smells", operator="GT", threshold=10),
    ]

    def __init__(self, config: QualityGateConfig | None = None):
        """Initialize quality gate.

        Args:
            config: Quality gate configuration. Uses defaults if not provided.
        """
        self.name = config.name if config else "default"
        self.conditions = config.conditions if config else self.DEFAULT_CONDITIONS

    def evaluate(self, results: AnalysisResults) -> QualityGateResult:
        """Evaluate quality gate against analysis results.

        Args:
            results: Analysis results to check.

        Returns:
            QualityGateResult with status and details.
        """
        condition_results = []
        all_passed = True
        has_warnings = False

        for condition in self.conditions:
            result = self._evaluate_condition(condition, results)
            condition_results.append({
                "metric": condition.metric,
                "operator": condition.operator,
                "threshold": condition.threshold,
                "actual": result.actual_value,
                "passed": result.passed,
                "message": result.message,
            })

            if not result.passed:
                all_passed = False

        # Determine overall status
        if all_passed:
            status = QualityGateStatus.PASSED
            message = "Quality gate passed"
        else:
            failed_count = sum(1 for c in condition_results if not c["passed"])
            status = QualityGateStatus.FAILED
            message = f"Quality gate failed: {failed_count} condition(s) not met"

        return QualityGateResult(
            status=status,
            conditions=condition_results,
            message=message,
        )

    def _evaluate_condition(
        self, condition: QualityGateCondition, results: AnalysisResults
    ) -> ConditionResult:
        """Evaluate a single condition.

        Args:
            condition: Condition to evaluate.
            results: Analysis results.

        Returns:
            ConditionResult with evaluation outcome.
        """
        # Get the actual value for the metric
        actual = self._get_metric_value(condition.metric, results)

        if actual is None:
            return ConditionResult(
                condition=condition,
                actual_value=None,
                passed=True,  # Can't fail if metric not available
                message=f"Metric '{condition.metric}' not available",
            )

        # Evaluate the condition
        threshold = condition.threshold
        passed = self._compare(actual, condition.operator, threshold)

        # Generate message
        if passed:
            message = f"{condition.metric} = {actual} (threshold: {condition.operator} {threshold})"
        else:
            message = f"{condition.metric} = {actual} fails condition {condition.operator} {threshold}"

        return ConditionResult(
            condition=condition,
            actual_value=actual,
            passed=passed,
            message=message,
        )

    def _get_metric_value(self, metric: str, results: AnalysisResults) -> Any:
        """Get the value of a metric from results.

        Args:
            metric: Metric name.
            results: Analysis results.

        Returns:
            Metric value or None if not found.
        """
        metrics = results.metrics

        metric_map = {
            "bugs": metrics.bugs_count,
            "bugs_count": metrics.bugs_count,
            "vulnerabilities": metrics.vulnerabilities_count,
            "vulnerabilities_count": metrics.vulnerabilities_count,
            "code_smells": metrics.code_smells_count,
            "code_smells_count": metrics.code_smells_count,
            "hotspots": metrics.hotspots_count,
            "hotspots_count": metrics.hotspots_count,
            "total_issues": metrics.total_issues,
            "issues": metrics.total_issues,
            "coverage": metrics.coverage_percent,
            "coverage_percent": metrics.coverage_percent,
            "duplications": metrics.duplications_percent,
            "duplications_percent": metrics.duplications_percent,
            "technical_debt": metrics.technical_debt_minutes,
            "technical_debt_minutes": metrics.technical_debt_minutes,
            "debt_ratio": metrics.technical_debt_ratio,
            "technical_debt_ratio": metrics.technical_debt_ratio,
            "lines_of_code": metrics.total_lines_of_code,
            "loc": metrics.total_lines_of_code,
            "reliability_rating": self._rating_to_number(metrics.reliability_rating),
            "security_rating": self._rating_to_number(metrics.security_rating),
            "maintainability_rating": self._rating_to_number(metrics.maintainability_rating),
            "complexity": metrics.average_complexity,
            "average_complexity": metrics.average_complexity,
        }

        return metric_map.get(metric.lower())

    def _rating_to_number(self, rating: str) -> int:
        """Convert rating letter to number for comparison.

        A=1, B=2, C=3, D=4, E=5
        """
        return {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5}.get(rating, 5)

    def _compare(self, actual: Any, operator: str, threshold: Any) -> bool:
        """Compare actual value against threshold.

        For conditions like "bugs > 0", we check if actual > threshold.
        If actual > threshold, the condition FAILS (we want 0 bugs).

        So the logic is inverted: condition passes if NOT (actual op threshold)

        Args:
            actual: Actual metric value.
            operator: Comparison operator.
            threshold: Threshold value.

        Returns:
            True if condition passes (actual does NOT violate threshold).
        """
        # Handle string thresholds (ratings)
        if isinstance(threshold, str) and threshold in "ABCDE":
            threshold = self._rating_to_number(threshold)

        # The operators specify what we DON'T want
        # GT means "fail if greater than" -> pass if NOT greater than
        operators = {
            "GT": lambda a, t: not (a > t),      # Fail if > threshold
            "GTE": lambda a, t: not (a >= t),   # Fail if >= threshold
            "LT": lambda a, t: not (a < t),      # Fail if < threshold
            "LTE": lambda a, t: not (a <= t),   # Fail if <= threshold
            "EQ": lambda a, t: not (a == t),     # Fail if equal
            "NE": lambda a, t: not (a != t),     # Fail if not equal
        }

        comparator = operators.get(operator.upper())
        if comparator:
            return comparator(actual, threshold)

        return True  # Unknown operator, assume pass


def evaluate_quality_gate(
    results: AnalysisResults,
    config: QualityGateConfig | None = None,
) -> QualityGateResult:
    """Convenience function to evaluate quality gate.

    Args:
        results: Analysis results.
        config: Optional quality gate configuration.

    Returns:
        QualityGateResult.
    """
    gate = QualityGate(config)
    return gate.evaluate(results)
