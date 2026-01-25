"""Python parser using AST module."""

import ast
from pathlib import Path
from typing import Any

from codescope.parsers.base import LanguageParser
from codescope.parsers.models import (
    ParsedFile,
    SymbolTable,
    FunctionDef,
    ClassDef,
    Import,
    Variable,
    Parameter,
    ASTNode,
)
from codescope.parsers.registry import ParserRegistry


@ParserRegistry.register
class PythonParser(LanguageParser):
    """Parser for Python source code using the ast module."""

    @property
    def language_id(self) -> str:
        return "python"

    @property
    def file_extensions(self) -> list[str]:
        return [".py", ".pyw", ".pyi"]

    def parse(self, source: str, file_path: Path | None = None) -> ParsedFile:
        """Parse Python source code."""
        path = file_path or Path("<string>")
        errors: list[str] = []
        symbols = SymbolTable()
        root_node: ASTNode | None = None

        try:
            tree = ast.parse(source)
            root_node = self._convert_ast(tree, source)
            symbols = self._extract_symbols(tree, source)
        except SyntaxError as e:
            errors.append(f"Syntax error at line {e.lineno}: {e.msg}")

        return ParsedFile(
            path=path,
            language="python",
            source=source,
            ast=root_node,
            symbols=symbols,
            errors=errors,
        )

    def _convert_ast(self, tree: ast.AST, source: str) -> ASTNode:
        """Convert Python AST to our ASTNode format."""
        lines = source.split("\n")

        def get_text(node: ast.AST) -> str:
            """Extract source text for a node."""
            if hasattr(node, "lineno") and hasattr(node, "end_lineno"):
                start_line = node.lineno - 1
                end_line = node.end_lineno
                if start_line == end_line - 1:
                    line = lines[start_line] if start_line < len(lines) else ""
                    start_col = getattr(node, "col_offset", 0)
                    end_col = getattr(node, "end_col_offset", len(line))
                    return line[start_col:end_col]
                else:
                    return "\n".join(lines[start_line:end_line])
            return ""

        def convert_node(node: ast.AST, parent: ASTNode | None = None) -> ASTNode:
            """Recursively convert AST nodes."""
            ast_node = ASTNode(
                type=node.__class__.__name__,
                text=get_text(node),
                start_line=getattr(node, "lineno", 0),
                end_line=getattr(node, "end_lineno", 0) or getattr(node, "lineno", 0),
                start_column=getattr(node, "col_offset", 0),
                end_column=getattr(node, "end_col_offset", 0),
                parent=parent,
            )

            for child in ast.iter_child_nodes(node):
                child_node = convert_node(child, ast_node)
                ast_node.children.append(child_node)

            return ast_node

        return convert_node(tree)

    def _extract_symbols(self, tree: ast.AST, source: str) -> SymbolTable:
        """Extract symbols from AST."""
        symbols = SymbolTable()
        lines = source.split("\n")

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    symbols.imports.append(
                        Import(
                            module=alias.name,
                            alias=alias.asname,
                            is_from_import=False,
                            line=node.lineno,
                        )
                    )

            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [alias.name for alias in node.names]
                symbols.imports.append(
                    Import(
                        module=module,
                        names=names,
                        is_from_import=True,
                        line=node.lineno,
                    )
                )

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = self._extract_function(node, source)
                # Check if it's a method (inside a class)
                for parent in ast.walk(tree):
                    if isinstance(parent, ast.ClassDef):
                        if node in parent.body:
                            func.is_method = True
                            break
                if not func.is_method:
                    symbols.functions.append(func)

            elif isinstance(node, ast.ClassDef):
                cls = self._extract_class(node, source)
                symbols.classes.append(cls)

            elif isinstance(node, ast.Assign):
                # Module-level assignments
                if isinstance(node, ast.Assign) and hasattr(node, "lineno"):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            value_text = self._get_node_source(node.value, lines)
                            symbols.variables.append(
                                Variable(
                                    name=target.id,
                                    line=node.lineno,
                                    value=value_text[:100] if value_text else None,
                                    scope="module",
                                )
                            )

        return symbols

    def _extract_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, source: str
    ) -> FunctionDef:
        """Extract function definition details."""
        lines = source.split("\n")

        # Extract parameters
        params = []
        for arg in node.args.args:
            type_hint = None
            if arg.annotation:
                type_hint = self._get_node_source(arg.annotation, lines)
            params.append(
                Parameter(
                    name=arg.arg,
                    type_hint=type_hint,
                )
            )

        # Handle *args
        if node.args.vararg:
            params.append(
                Parameter(
                    name=node.args.vararg.arg,
                    is_args=True,
                )
            )

        # Handle **kwargs
        if node.args.kwarg:
            params.append(
                Parameter(
                    name=node.args.kwarg.arg,
                    is_kwargs=True,
                )
            )

        # Extract return type
        return_type = None
        if node.returns:
            return_type = self._get_node_source(node.returns, lines)

        # Extract decorators
        decorators = []
        for dec in node.decorator_list:
            dec_text = self._get_node_source(dec, lines)
            if dec_text:
                decorators.append(dec_text)

        # Extract docstring
        docstring = ast.get_docstring(node)

        # Calculate body lines
        end_line = node.end_lineno or node.lineno
        body_start = node.body[0].lineno if node.body else node.lineno + 1
        body_lines = end_line - body_start + 1

        return FunctionDef(
            name=node.name,
            start_line=node.lineno,
            end_line=end_line,
            parameters=params,
            return_type=return_type,
            decorators=decorators,
            docstring=docstring,
            is_async=isinstance(node, ast.AsyncFunctionDef),
            body_lines=body_lines,
            complexity=self._calculate_complexity(node),
        )

    def _extract_class(self, node: ast.ClassDef, source: str) -> ClassDef:
        """Extract class definition details."""
        lines = source.split("\n")

        # Extract base classes
        bases = []
        for base in node.bases:
            base_text = self._get_node_source(base, lines)
            if base_text:
                bases.append(base_text)

        # Extract decorators
        decorators = []
        for dec in node.decorator_list:
            dec_text = self._get_node_source(dec, lines)
            if dec_text:
                decorators.append(dec_text)

        # Extract docstring
        docstring = ast.get_docstring(node)

        # Extract methods
        methods = []
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method = self._extract_function(item, source)
                method.is_method = True
                methods.append(method)

        # Extract class attributes
        attributes = []
        for item in node.body:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        attributes.append(target.id)
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                attributes.append(item.target.id)

        return ClassDef(
            name=node.name,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            bases=bases,
            decorators=decorators,
            docstring=docstring,
            methods=methods,
            attributes=attributes,
        )

    def _get_node_source(self, node: ast.AST, lines: list[str]) -> str:
        """Get source code for a node."""
        if not hasattr(node, "lineno"):
            return ""

        try:
            if hasattr(node, "end_lineno") and node.end_lineno:
                if node.lineno == node.end_lineno:
                    line = lines[node.lineno - 1]
                    start = getattr(node, "col_offset", 0)
                    end = getattr(node, "end_col_offset", len(line))
                    return line[start:end]
                else:
                    result_lines = []
                    for i in range(node.lineno - 1, node.end_lineno):
                        if i < len(lines):
                            result_lines.append(lines[i])
                    return "\n".join(result_lines)
            else:
                return lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        except (IndexError, AttributeError):
            return ""

    def _calculate_complexity(self, node: ast.AST) -> int:
        """Calculate cyclomatic complexity for a function."""
        complexity = 1  # Base complexity

        for child in ast.walk(node):
            # Decision points that increase complexity
            if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor)):
                complexity += 1
            elif isinstance(child, ast.ExceptHandler):
                complexity += 1
            elif isinstance(child, ast.With, ):
                complexity += 1
            elif isinstance(child, ast.Assert):
                complexity += 1
            elif isinstance(child, ast.comprehension):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                # and/or operations
                complexity += len(child.values) - 1
            elif isinstance(child, ast.IfExp):
                # Ternary operator
                complexity += 1

        return complexity
