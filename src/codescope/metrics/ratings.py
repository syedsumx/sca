"""Rating calculations for code quality metrics."""


def calculate_reliability_rating(bugs_count: int) -> str:
    """Calculate reliability rating based on bug count.

    Rating scale:
    - A: 0 bugs
    - B: 1 bug
    - C: 2-5 bugs
    - D: 6-10 bugs
    - E: 11+ bugs

    Args:
        bugs_count: Number of bugs found.

    Returns:
        Rating letter (A-E).
    """
    if bugs_count == 0:
        return "A"
    elif bugs_count == 1:
        return "B"
    elif bugs_count <= 5:
        return "C"
    elif bugs_count <= 10:
        return "D"
    return "E"


def calculate_security_rating(vulnerabilities_count: int) -> str:
    """Calculate security rating based on vulnerability count.

    Rating scale:
    - A: 0 vulnerabilities
    - B: 1 vulnerability
    - C: 2-5 vulnerabilities
    - D: 6-10 vulnerabilities
    - E: 11+ vulnerabilities

    Args:
        vulnerabilities_count: Number of vulnerabilities found.

    Returns:
        Rating letter (A-E).
    """
    if vulnerabilities_count == 0:
        return "A"
    elif vulnerabilities_count == 1:
        return "B"
    elif vulnerabilities_count <= 5:
        return "C"
    elif vulnerabilities_count <= 10:
        return "D"
    return "E"


def calculate_maintainability_rating(technical_debt_ratio: float) -> str:
    """Calculate maintainability rating based on technical debt ratio.

    Technical debt ratio = (remediation cost / development cost) * 100

    Rating scale:
    - A: 0-5% debt ratio
    - B: 6-10% debt ratio
    - C: 11-20% debt ratio
    - D: 21-50% debt ratio
    - E: 51%+ debt ratio

    Args:
        technical_debt_ratio: Technical debt ratio as percentage.

    Returns:
        Rating letter (A-E).
    """
    if technical_debt_ratio <= 5:
        return "A"
    elif technical_debt_ratio <= 10:
        return "B"
    elif technical_debt_ratio <= 20:
        return "C"
    elif technical_debt_ratio <= 50:
        return "D"
    return "E"


def calculate_coverage_rating(coverage_percent: float | None) -> str:
    """Calculate coverage rating.

    Rating scale:
    - A: 80%+ coverage
    - B: 70-79% coverage
    - C: 50-69% coverage
    - D: 30-49% coverage
    - E: <30% coverage

    Args:
        coverage_percent: Code coverage percentage.

    Returns:
        Rating letter (A-E).
    """
    if coverage_percent is None:
        return "-"
    if coverage_percent >= 80:
        return "A"
    elif coverage_percent >= 70:
        return "B"
    elif coverage_percent >= 50:
        return "C"
    elif coverage_percent >= 30:
        return "D"
    return "E"


def calculate_duplication_rating(duplication_percent: float) -> str:
    """Calculate duplication rating.

    Rating scale:
    - A: 0-3% duplication
    - B: 3-5% duplication
    - C: 5-10% duplication
    - D: 10-20% duplication
    - E: 20%+ duplication

    Args:
        duplication_percent: Code duplication percentage.

    Returns:
        Rating letter (A-E).
    """
    if duplication_percent <= 3:
        return "A"
    elif duplication_percent <= 5:
        return "B"
    elif duplication_percent <= 10:
        return "C"
    elif duplication_percent <= 20:
        return "D"
    return "E"


def get_rating_color(rating: str) -> str:
    """Get color associated with a rating.

    Args:
        rating: Rating letter.

    Returns:
        Color name for terminal output.
    """
    colors = {
        "A": "green",
        "B": "bright_green",
        "C": "yellow",
        "D": "orange",
        "E": "red",
        "-": "dim",
    }
    return colors.get(rating, "white")


def rating_to_score(rating: str) -> int:
    """Convert rating letter to numeric score.

    Args:
        rating: Rating letter (A-E).

    Returns:
        Numeric score (5 for A, 1 for E).
    """
    scores = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1, "-": 0}
    return scores.get(rating, 0)


def score_to_rating(score: float) -> str:
    """Convert numeric score to rating letter.

    Args:
        score: Numeric score (1-5).

    Returns:
        Rating letter (A-E).
    """
    if score >= 4.5:
        return "A"
    elif score >= 3.5:
        return "B"
    elif score >= 2.5:
        return "C"
    elif score >= 1.5:
        return "D"
    return "E"
