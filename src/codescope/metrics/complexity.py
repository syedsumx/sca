"""Complexity metrics calculation."""

import ast
from typing import Any

from codescope.parsers.models import ParsedFile


def calculate_cyclomatic_complexity(source: str) -> int:
    """Calculate cyclomatic complexity for source code.

    Cyclomatic complexity = E - N + 2P
    Where E = edges, N = nodes, P = connected components

    Simplified: count decision points + 1

    Args:
        source: Python source code.

    Returns:
        Cyclomatic complexity score.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 1

    complexity = 1  # Base complexity

    for node in ast.walk(tree):
        # Decision points
        if isinstance(node, ast.If):
            complexity += 1
        elif isinstance(node, ast.For):
            complexity += 1
        elif isinstance(node, ast.AsyncFor):
            complexity += 1
        elif isinstance(node, ast.While):
            complexity += 1
        elif isinstance(node, ast.ExceptHandler):
            complexity += 1
        elif isinstance(node, ast.With):
            complexity += 1
        elif isinstance(node, ast.AsyncWith):
            complexity += 1
        elif isinstance(node, ast.Assert):
            complexity += 1
        elif isinstance(node, ast.BoolOp):
            # and/or add complexity
            complexity += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            complexity += 1
            # 'if' clauses in comprehensions
            complexity += len(node.ifs)
        elif isinstance(node, ast.IfExp):
            # Ternary expressions
            complexity += 1

    return complexity


def calculate_cognitive_complexity(file: ParsedFile, func_line: int | None = None) -> int:
    """Calculate cognitive complexity for a file or function.

    Cognitive complexity measures how difficult code is to understand,
    taking into account:
    - Nesting depth
    - Breaks in linear flow
    - Boolean operators

    Args:
        file: Parsed file.
        func_line: Optional line number of function to analyze.

    Returns:
        Cognitive complexity score.
    """
    try:
        tree = ast.parse(file.source)
    except SyntaxError:
        return 0

    if func_line is not None:
        # Find the specific function
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.lineno == func_line:
                    return _calculate_cognitive_for_node(node)
        return 0
    else:
        # Calculate for entire file
        total = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                total += _calculate_cognitive_for_node(node)
        return total


def _calculate_cognitive_for_node(func_node: ast.AST) -> int:
    """Calculate cognitive complexity for a function node."""
    complexity = 0

    def increment_for_nesting(node: ast.AST, nesting: int) -> int:
        """Process a node and return complexity increment."""
        nonlocal complexity
        local_increment = 0

        # Structural increment (adds to complexity)
        if isinstance(node, (ast.If, ast.For, ast.While, ast.AsyncFor)):
            # +1 for the construct itself
            local_increment += 1
            # +1 for each level of nesting
            local_increment += nesting

        elif isinstance(node, ast.ExceptHandler):
            local_increment += 1 + nesting

        elif isinstance(node, (ast.Match,)) if hasattr(ast, 'Match') else False:
            # Python 3.10+ match statement
            local_increment += 1 + nesting

        # Hybrid increment (adds but doesn't affect nesting)
        elif isinstance(node, ast.BoolOp):
            # Each sequence of and/or
            local_increment += len(node.values) - 1

        elif isinstance(node, ast.IfExp):
            # Ternary operator
            local_increment += 1

        # Recursion
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.func.id == func_node.name:
                        local_increment += 1

        return local_increment

    def walk_with_nesting(node: ast.AST, nesting: int) -> None:
        """Walk the AST tracking nesting depth."""
        nonlocal complexity

        # Calculate increment for this node
        complexity += increment_for_nesting(node, nesting)

        # Determine new nesting level
        new_nesting = nesting
        if isinstance(node, (ast.If, ast.For, ast.While, ast.AsyncFor, ast.ExceptHandler)):
            new_nesting = nesting + 1

        # Process children
        for child in ast.iter_child_nodes(node):
            walk_with_nesting(child, new_nesting)

    # Walk all children of the function
    for child in ast.iter_child_nodes(func_node):
        walk_with_nesting(child, 0)

    return complexity


def calculate_halstead_metrics(source: str) -> dict[str, float]:
    """Calculate Halstead metrics for source code.

    Args:
        source: Python source code.

    Returns:
        Dictionary with Halstead metrics:
        - n1: Number of distinct operators
        - n2: Number of distinct operands
        - N1: Total number of operators
        - N2: Total number of operands
        - vocabulary: n1 + n2
        - length: N1 + N2
        - volume: N * log2(n)
        - difficulty: (n1/2) * (N2/n2)
        - effort: D * V
        - time: E / 18 (seconds)
        - bugs: V / 3000
    """
    import math

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _empty_halstead()

    operators: set[str] = set()
    operands: set[str] = set()
    operator_count = 0
    operand_count = 0

    # Operator node types
    operator_types = {
        ast.Add: "+",
        ast.Sub: "-",
        ast.Mult: "*",
        ast.Div: "/",
        ast.FloorDiv: "//",
        ast.Mod: "%",
        ast.Pow: "**",
        ast.LShift: "<<",
        ast.RShift: ">>",
        ast.BitOr: "|",
        ast.BitXor: "^",
        ast.BitAnd: "&",
        ast.MatMult: "@",
        ast.Eq: "==",
        ast.NotEq: "!=",
        ast.Lt: "<",
        ast.LtE: "<=",
        ast.Gt: ">",
        ast.GtE: ">=",
        ast.Is: "is",
        ast.IsNot: "is not",
        ast.In: "in",
        ast.NotIn: "not in",
        ast.And: "and",
        ast.Or: "or",
        ast.Not: "not",
        ast.Invert: "~",
        ast.UAdd: "+",
        ast.USub: "-",
    }

    for node in ast.walk(tree):
        # Count operators
        if type(node) in operator_types:
            op = operator_types[type(node)]
            operators.add(op)
            operator_count += 1

        # Keywords as operators
        elif isinstance(node, (ast.If, ast.While, ast.For, ast.With, ast.Try)):
            keyword = node.__class__.__name__.lower()
            operators.add(keyword)
            operator_count += 1

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            operators.add("def")
            operator_count += 1
            # Function name is operand
            operands.add(node.name)
            operand_count += 1

        elif isinstance(node, ast.ClassDef):
            operators.add("class")
            operator_count += 1
            operands.add(node.name)
            operand_count += 1

        elif isinstance(node, ast.Return):
            operators.add("return")
            operator_count += 1

        elif isinstance(node, ast.Assign):
            operators.add("=")
            operator_count += 1

        # Count operands
        elif isinstance(node, ast.Name):
            operands.add(node.id)
            operand_count += 1

        elif isinstance(node, ast.Constant):
            operands.add(str(node.value))
            operand_count += 1

    n1 = len(operators)
    n2 = len(operands)
    N1 = operator_count
    N2 = operand_count

    if n1 == 0 or n2 == 0:
        return _empty_halstead()

    n = n1 + n2  # Vocabulary
    N = N1 + N2  # Length

    volume = N * math.log2(n) if n > 0 else 0
    difficulty = (n1 / 2) * (N2 / n2) if n2 > 0 else 0
    effort = difficulty * volume
    time_seconds = effort / 18
    bugs = volume / 3000

    return {
        "n1": n1,
        "n2": n2,
        "N1": N1,
        "N2": N2,
        "vocabulary": n,
        "length": N,
        "volume": round(volume, 2),
        "difficulty": round(difficulty, 2),
        "effort": round(effort, 2),
        "time": round(time_seconds, 2),
        "bugs": round(bugs, 3),
    }


def _empty_halstead() -> dict[str, float]:
    """Return empty Halstead metrics."""
    return {
        "n1": 0,
        "n2": 0,
        "N1": 0,
        "N2": 0,
        "vocabulary": 0,
        "length": 0,
        "volume": 0,
        "difficulty": 0,
        "effort": 0,
        "time": 0,
        "bugs": 0,
    }
