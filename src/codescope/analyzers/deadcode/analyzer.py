"""Dead code detection analyzer."""

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class DeadCodeItem:
    """A piece of potentially dead code."""

    item_type: str  # 'function', 'class', 'variable', 'import', 'parameter'
    name: str
    file_path: str
    line_number: int
    end_line: int
    confidence: float  # 0.0 to 1.0
    reason: str

    def to_dict(self) -> dict:
        return {
            "item_type": self.item_type,
            "name": self.name,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "end_line": self.end_line,
            "confidence": self.confidence,
            "reason": self.reason,
        }


@dataclass
class DeadCodeResult:
    """Result of dead code analysis."""

    files_analyzed: int = 0
    dead_code_items: list[DeadCodeItem] = field(default_factory=list)

    @property
    def total_dead_items(self) -> int:
        return len(self.dead_code_items)

    @property
    def dead_functions(self) -> list[DeadCodeItem]:
        return [item for item in self.dead_code_items if item.item_type == 'function']

    @property
    def dead_classes(self) -> list[DeadCodeItem]:
        return [item for item in self.dead_code_items if item.item_type == 'class']

    @property
    def dead_imports(self) -> list[DeadCodeItem]:
        return [item for item in self.dead_code_items if item.item_type == 'import']

    @property
    def dead_variables(self) -> list[DeadCodeItem]:
        return [item for item in self.dead_code_items if item.item_type == 'variable']

    def to_dict(self) -> dict:
        return {
            "files_analyzed": self.files_analyzed,
            "total_dead_items": self.total_dead_items,
            "dead_functions_count": len(self.dead_functions),
            "dead_classes_count": len(self.dead_classes),
            "dead_imports_count": len(self.dead_imports),
            "dead_variables_count": len(self.dead_variables),
            "dead_code_items": [item.to_dict() for item in self.dead_code_items],
        }


class DeadCodeAnalyzer:
    """Analyzer for detecting potentially dead/unused code."""

    def __init__(self, confidence_threshold: float = 0.7):
        self.confidence_threshold = confidence_threshold

    def analyze(self, path: Path) -> DeadCodeResult:
        """Analyze a path for dead code."""
        result = DeadCodeResult()

        if path.is_file():
            items = self._analyze_file(path)
            result.dead_code_items.extend(items)
            result.files_analyzed = 1
        else:
            # Collect all definitions and usages across project
            all_definitions: dict[str, list[tuple[str, int, int]]] = {}  # name -> [(file, line, end_line)]
            all_usages: set[str] = set()

            files = list(path.rglob("*.py"))

            # First pass: collect all definitions and usages
            for file_path in files:
                if self._should_skip(file_path):
                    continue

                try:
                    content = file_path.read_text(encoding='utf-8', errors='ignore')
                    tree = ast.parse(content)

                    defs, uses = self._collect_definitions_and_usages(tree, str(file_path))

                    for name, info in defs.items():
                        if name not in all_definitions:
                            all_definitions[name] = []
                        all_definitions[name].extend(info)

                    all_usages.update(uses)

                except Exception:
                    continue

            # Second pass: find unused definitions
            for name, locations in all_definitions.items():
                if name not in all_usages and not self._is_special_name(name):
                    for file_path, line, end_line in locations:
                        item_type = self._determine_item_type(name, file_path, line)
                        result.dead_code_items.append(DeadCodeItem(
                            item_type=item_type,
                            name=name,
                            file_path=file_path,
                            line_number=line,
                            end_line=end_line,
                            confidence=0.8,
                            reason=f"'{name}' is defined but never used in the project",
                        ))

            # Also analyze individual files for local dead code
            for file_path in files:
                if self._should_skip(file_path):
                    continue

                items = self._analyze_file(file_path)
                result.dead_code_items.extend(items)
                result.files_analyzed += 1

        # Filter by confidence threshold
        result.dead_code_items = [
            item for item in result.dead_code_items
            if item.confidence >= self.confidence_threshold
        ]

        # Remove duplicates
        seen = set()
        unique_items = []
        for item in result.dead_code_items:
            key = (item.name, item.file_path, item.line_number)
            if key not in seen:
                seen.add(key)
                unique_items.append(item)

        result.dead_code_items = unique_items

        return result

    def _analyze_file(self, file_path: Path) -> list[DeadCodeItem]:
        """Analyze a single file for dead code."""
        items = []

        try:
            content = file_path.read_text(encoding='utf-8', errors='ignore')
            tree = ast.parse(content)
        except Exception:
            return items

        # Find unused imports
        items.extend(self._find_unused_imports(tree, str(file_path), content))

        # Find unused local variables
        items.extend(self._find_unused_variables(tree, str(file_path)))

        # Find unreachable code
        items.extend(self._find_unreachable_code(tree, str(file_path)))

        return items

    def _find_unused_imports(self, tree: ast.AST, file_path: str, content: str) -> list[DeadCodeItem]:
        """Find unused imports in a file."""
        items = []

        # Collect imports
        imports: list[tuple[str, int, int]] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname if alias.asname else alias.name
                    imports.append((name, node.lineno, node.end_lineno or node.lineno))
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == '*':
                        continue
                    name = alias.asname if alias.asname else alias.name
                    imports.append((name, node.lineno, node.end_lineno or node.lineno))

        # Check usage
        for name, line, end_line in imports:
            # Simple heuristic: count occurrences (excluding import line)
            lines = content.split('\n')
            usage_count = 0

            for i, ln in enumerate(lines):
                if i + 1 == line:
                    continue
                # Look for word boundary matches
                if re.search(rf'\b{re.escape(name)}\b', ln):
                    usage_count += 1

            if usage_count == 0:
                items.append(DeadCodeItem(
                    item_type='import',
                    name=name,
                    file_path=file_path,
                    line_number=line,
                    end_line=end_line,
                    confidence=0.9,
                    reason=f"Import '{name}' is never used in this file",
                ))

        return items

    def _find_unused_variables(self, tree: ast.AST, file_path: str) -> list[DeadCodeItem]:
        """Find unused local variables."""
        items = []

        class VariableVisitor(ast.NodeVisitor):
            def __init__(self):
                self.scopes: list[dict[str, tuple[int, int]]] = [{}]
                self.usages: list[set[str]] = [set()]
                self.results: list[DeadCodeItem] = []

            def visit_FunctionDef(self, node):
                self._visit_function(node)

            def visit_AsyncFunctionDef(self, node):
                self._visit_function(node)

            def _visit_function(self, node):
                # New scope
                self.scopes.append({})
                self.usages.append(set())

                # Add parameters to scope
                for arg in node.args.args + node.args.kwonlyargs:
                    self.scopes[-1][arg.arg] = (node.lineno, node.end_lineno or node.lineno)

                if node.args.vararg:
                    self.scopes[-1][node.args.vararg.arg] = (node.lineno, node.end_lineno or node.lineno)

                if node.args.kwarg:
                    self.scopes[-1][node.args.kwarg.arg] = (node.lineno, node.end_lineno or node.lineno)

                # Visit body
                for child in node.body:
                    self.visit(child)

                # Check for unused variables
                scope = self.scopes.pop()
                usage = self.usages.pop()

                for name, (line, end_line) in scope.items():
                    if name not in usage and not name.startswith('_'):
                        self.results.append(DeadCodeItem(
                            item_type='variable',
                            name=name,
                            file_path=file_path,
                            line_number=line,
                            end_line=end_line,
                            confidence=0.75,
                            reason=f"Variable '{name}' is assigned but never used",
                        ))

            def visit_Assign(self, node):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.scopes[-1][target.id] = (node.lineno, node.end_lineno or node.lineno)
                self.generic_visit(node)

            def visit_Name(self, node):
                if isinstance(node.ctx, ast.Load):
                    self.usages[-1].add(node.id)
                self.generic_visit(node)

        visitor = VariableVisitor()
        visitor.visit(tree)
        items.extend(visitor.results)

        return items

    def _find_unreachable_code(self, tree: ast.AST, file_path: str) -> list[DeadCodeItem]:
        """Find unreachable code after return/raise/break/continue."""
        items = []

        class UnreachableVisitor(ast.NodeVisitor):
            def __init__(self):
                self.results: list[DeadCodeItem] = []

            def check_body(self, body: list[ast.stmt]):
                for i, stmt in enumerate(body):
                    if isinstance(stmt, (ast.Return, ast.Raise)):
                        # Check if there's code after this
                        remaining = body[i + 1:]
                        if remaining:
                            # Filter out pass statements
                            meaningful = [s for s in remaining if not isinstance(s, ast.Pass)]
                            if meaningful:
                                first = meaningful[0]
                                last = meaningful[-1]
                                self.results.append(DeadCodeItem(
                                    item_type='code',
                                    name='unreachable code',
                                    file_path=file_path,
                                    line_number=first.lineno,
                                    end_line=last.end_lineno or last.lineno,
                                    confidence=0.95,
                                    reason="Code after return/raise is unreachable",
                                ))
                        break
                    self.visit(stmt)

            def visit_FunctionDef(self, node):
                self.check_body(node.body)

            def visit_AsyncFunctionDef(self, node):
                self.check_body(node.body)

            def visit_If(self, node):
                self.check_body(node.body)
                self.check_body(node.orelse)

            def visit_For(self, node):
                self.check_body(node.body)
                self.check_body(node.orelse)

            def visit_While(self, node):
                self.check_body(node.body)
                self.check_body(node.orelse)

            def visit_Try(self, node):
                self.check_body(node.body)
                self.check_body(node.finalbody)
                for handler in node.handlers:
                    self.check_body(handler.body)

        visitor = UnreachableVisitor()
        visitor.visit(tree)
        items.extend(visitor.results)

        return items

    def _collect_definitions_and_usages(
        self, tree: ast.AST, file_path: str
    ) -> tuple[dict[str, list[tuple[str, int, int]]], set[str]]:
        """Collect all definitions and usages from an AST."""
        definitions: dict[str, list[tuple[str, int, int]]] = {}
        usages: set[str] = set()

        for node in ast.walk(tree):
            # Definitions
            if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                if node.name not in definitions:
                    definitions[node.name] = []
                definitions[node.name].append((file_path, node.lineno, node.end_lineno or node.lineno))

            elif isinstance(node, ast.ClassDef):
                if node.name not in definitions:
                    definitions[node.name] = []
                definitions[node.name].append((file_path, node.lineno, node.end_lineno or node.lineno))

            # Usages
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                usages.add(node.id)

            elif isinstance(node, ast.Attribute):
                usages.add(node.attr)

            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    usages.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    usages.add(node.func.attr)

        return definitions, usages

    def _should_skip(self, file_path: Path) -> bool:
        """Check if a file should be skipped."""
        skip_patterns = [
            '__pycache__',
            '.git',
            'node_modules',
            '.venv',
            'venv',
            'test_',
            '_test.py',
            'tests/',
            'migrations/',
        ]

        path_str = str(file_path)
        return any(pattern in path_str for pattern in skip_patterns)

    def _is_special_name(self, name: str) -> bool:
        """Check if name is a special Python name that shouldn't be flagged."""
        special_patterns = [
            '__',  # Dunder methods
            'main',
            'setup',
            'app',
            'application',
        ]

        return any(pattern in name for pattern in special_patterns) or name.startswith('_')

    def _determine_item_type(self, name: str, file_path: str, line: int) -> str:
        """Determine the type of a definition."""
        try:
            content = Path(file_path).read_text(encoding='utf-8', errors='ignore')
            lines = content.split('\n')
            if line <= len(lines):
                ln = lines[line - 1].strip()
                if ln.startswith('def ') or ln.startswith('async def '):
                    return 'function'
                elif ln.startswith('class '):
                    return 'class'
        except Exception:
            pass

        return 'function'


def analyze_dead_code(path: Path, confidence_threshold: float = 0.7) -> DeadCodeResult:
    """Convenience function to analyze dead code."""
    analyzer = DeadCodeAnalyzer(confidence_threshold=confidence_threshold)
    return analyzer.analyze(path)
