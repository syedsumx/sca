"""Main metrics calculator for CodeScope."""

import math
from pathlib import Path

from codescope.core.models import FileMetrics
from codescope.core.utils import count_lines, detect_language
from codescope.parsers.models import ParsedFile
from codescope.metrics.complexity import (
    calculate_cyclomatic_complexity,
    calculate_cognitive_complexity,
)


class MetricsCalculator:
    """Calculator for code metrics."""

    def calculate_file_metrics(self, file: ParsedFile) -> FileMetrics:
        """Calculate all metrics for a parsed file.

        Args:
            file: Parsed source file.

        Returns:
            FileMetrics with calculated values.
        """
        # Get line counts
        total, code, comment, blank = count_lines(file.path)

        # Calculate complexity
        total_cyclomatic = 0
        total_cognitive = 0
        function_count = 0

        for func in file.symbols.get_all_functions():
            total_cyclomatic += func.complexity
            function_count += 1

        # Calculate average complexity
        avg_cyclomatic = total_cyclomatic // max(1, function_count)

        # Calculate cognitive complexity from AST
        if file.symbols.functions or file.symbols.classes:
            for func in file.symbols.get_all_functions():
                total_cognitive += calculate_cognitive_complexity(file, func.start_line)

        # Calculate maintainability index
        maintainability = self.calculate_maintainability_index(
            loc=code,
            cyclomatic_complexity=total_cyclomatic,
            halstead_volume=self._estimate_halstead_volume(file),
            comment_percentage=(comment / max(1, total)) * 100,
        )

        return FileMetrics(
            file_path=file.path,
            language=file.language,
            lines_of_code=code,
            logical_lines=code,  # Simplified
            comment_lines=comment,
            blank_lines=blank,
            cyclomatic_complexity=total_cyclomatic,
            cognitive_complexity=total_cognitive,
            maintainability_index=maintainability,
            functions_count=len(file.symbols.functions),
            classes_count=len(file.symbols.classes),
        )

    def calculate_maintainability_index(
        self,
        loc: int,
        cyclomatic_complexity: int,
        halstead_volume: float,
        comment_percentage: float = 0,
    ) -> float:
        """Calculate the maintainability index.

        Uses the Microsoft Visual Studio formula:
        MI = MAX(0, (171 - 5.2 * ln(V) - 0.23 * G - 16.2 * ln(L)) * 100 / 171)

        Where:
        - V = Halstead Volume
        - G = Cyclomatic Complexity
        - L = Lines of Code

        Args:
            loc: Lines of code.
            cyclomatic_complexity: Total cyclomatic complexity.
            halstead_volume: Halstead volume metric.
            comment_percentage: Percentage of comment lines.

        Returns:
            Maintainability index (0-100).
        """
        if loc == 0:
            return 100.0

        # Avoid log(0)
        volume = max(1, halstead_volume)
        lines = max(1, loc)
        complexity = max(1, cyclomatic_complexity)

        mi = 171 - 5.2 * math.log(volume) - 0.23 * complexity - 16.2 * math.log(lines)

        # Normalize to 0-100
        mi = max(0, mi * 100 / 171)

        # Add comment bonus (up to 5 points)
        if comment_percentage > 0:
            mi = min(100, mi + comment_percentage * 0.05)

        return round(mi, 2)

    def _estimate_halstead_volume(self, file: ParsedFile) -> float:
        """Estimate Halstead volume for a file.

        Simplified estimation based on code characteristics.

        Args:
            file: Parsed source file.

        Returns:
            Estimated Halstead volume.
        """
        # Count operators and operands (simplified)
        source = file.source

        # Common operators
        operators = [
            "+", "-", "*", "/", "//", "%", "**",
            "=", "==", "!=", "<", ">", "<=", ">=",
            "and", "or", "not", "in", "is",
            "(", ")", "[", "]", "{", "}", ",", ":", ".",
            "if", "else", "elif", "for", "while", "try", "except",
            "def", "class", "return", "import", "from",
        ]

        n1 = 0  # Unique operators
        n2 = 0  # Unique operands
        N1 = 0  # Total operators
        N2 = 0  # Total operands

        seen_operators = set()
        seen_operands = set()

        # Very simplified counting
        words = source.split()
        for word in words:
            word_clean = word.strip("()[]{}:,.")
            if word_clean in operators or any(op in word for op in operators[:15]):
                if word_clean not in seen_operators:
                    seen_operators.add(word_clean)
                    n1 += 1
                N1 += 1
            elif word_clean.isidentifier():
                if word_clean not in seen_operands:
                    seen_operands.add(word_clean)
                    n2 += 1
                N2 += 1

        # Halstead metrics
        n = n1 + n2  # Vocabulary
        N = N1 + N2  # Length

        if n == 0:
            return 0.0

        # Volume = N * log2(n)
        volume = N * math.log2(max(2, n))
        return volume

    def calculate_technical_debt_minutes(
        self,
        issues_effort: int,
        code_smells_count: int,
        complexity_excess: int,
    ) -> int:
        """Calculate total technical debt in minutes.

        Args:
            issues_effort: Sum of effort from all issues.
            code_smells_count: Number of code smells.
            complexity_excess: Amount by which complexity exceeds threshold.

        Returns:
            Total technical debt in minutes.
        """
        debt = issues_effort

        # Add complexity-based debt (5 min per excess point)
        debt += complexity_excess * 5

        return debt
