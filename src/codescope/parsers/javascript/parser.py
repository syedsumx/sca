"""JavaScript/TypeScript parser using regex-based analysis."""

import re
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
class JavaScriptParser(LanguageParser):
    """Parser for JavaScript/TypeScript source code."""

    @property
    def language_id(self) -> str:
        return "javascript"

    @property
    def file_extensions(self) -> list[str]:
        return [".js", ".jsx", ".mjs", ".cjs"]

    def parse(self, source: str, file_path: Path | None = None) -> ParsedFile:
        """Parse JavaScript source code."""
        path = file_path or Path("<string>")
        errors: list[str] = []
        symbols = SymbolTable()

        try:
            symbols = self._extract_symbols(source)
            root_node = self._build_ast(source)
        except Exception as e:
            errors.append(f"Parse error: {str(e)}")
            root_node = ASTNode(type="Program", text="", start_line=1, end_line=1)

        return ParsedFile(
            path=path,
            language="javascript",
            source=source,
            ast=root_node,
            symbols=symbols,
            errors=errors,
        )

    def _build_ast(self, source: str) -> ASTNode:
        """Build a simple AST structure."""
        lines = source.split("\n")
        root = ASTNode(
            type="Program",
            text=source,
            start_line=1,
            end_line=len(lines),
        )
        return root

    def _extract_symbols(self, source: str) -> SymbolTable:
        """Extract symbols from source code using regex."""
        symbols = SymbolTable()
        lines = source.split("\n")

        # Import patterns
        import_patterns = [
            r'import\s+(?:(?:\{[^}]+\})|(?:\*\s+as\s+\w+)|(?:\w+))\s+from\s+[\'"]([^\'"]+)[\'"]',
            r'import\s+[\'"]([^\'"]+)[\'"]',
            r'require\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)',
        ]

        # Function patterns
        func_patterns = [
            r'(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)',
            r'(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>',
            r'(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?function',
            r'(\w+)\s*:\s*(?:async\s+)?function\s*\([^)]*\)',
            r'(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{',  # Method shorthand
        ]

        # Class patterns
        class_pattern = r'class\s+(\w+)(?:\s+extends\s+(\w+))?'

        # Variable patterns
        var_pattern = r'(?:const|let|var)\s+(\w+)\s*='

        for i, line in enumerate(lines, 1):
            # Extract imports
            for pattern in import_patterns:
                for match in re.finditer(pattern, line):
                    symbols.imports.append(
                        Import(
                            module=match.group(1),
                            is_from_import=True,
                            line=i,
                        )
                    )

            # Extract functions
            for pattern in func_patterns:
                for match in re.finditer(pattern, line):
                    name = match.group(1)
                    if name and name not in ("if", "for", "while", "switch", "catch"):
                        params = []
                        if len(match.groups()) > 1 and match.group(2):
                            param_str = match.group(2)
                            for p in param_str.split(","):
                                p = p.strip()
                                if p:
                                    # Handle default values and destructuring
                                    param_name = re.split(r'[=:{}\[\]]', p)[0].strip()
                                    if param_name:
                                        params.append(Parameter(name=param_name))

                        # Find function end
                        end_line = self._find_block_end(lines, i - 1)

                        symbols.functions.append(
                            FunctionDef(
                                name=name,
                                start_line=i,
                                end_line=end_line,
                                parameters=params,
                                is_async="async" in line[:match.start()],
                            )
                        )

            # Extract classes
            for match in re.finditer(class_pattern, line):
                name = match.group(1)
                bases = [match.group(2)] if match.group(2) else []
                end_line = self._find_block_end(lines, i - 1)

                # Find methods within class
                methods = []
                class_body = "\n".join(lines[i:end_line])
                method_pattern = r'(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{'
                for m_match in re.finditer(method_pattern, class_body):
                    m_name = m_match.group(1)
                    if m_name not in ("if", "for", "while", "switch", "catch", "constructor"):
                        methods.append(
                            FunctionDef(
                                name=m_name,
                                start_line=i,
                                end_line=i,
                                is_method=True,
                            )
                        )

                symbols.classes.append(
                    ClassDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        bases=bases,
                        methods=methods,
                    )
                )

            # Extract variables (module level)
            for match in re.finditer(var_pattern, line):
                name = match.group(1)
                symbols.variables.append(
                    Variable(
                        name=name,
                        line=i,
                        scope="module",
                    )
                )

        return symbols

    def _find_block_end(self, lines: list[str], start_idx: int) -> int:
        """Find the end of a code block by matching braces."""
        brace_count = 0
        started = False

        for i in range(start_idx, len(lines)):
            line = lines[i]
            # Remove strings and comments for accurate brace counting
            line = re.sub(r'//.*$', '', line)
            line = re.sub(r'/\*.*?\*/', '', line)
            line = re.sub(r'"(?:[^"\\]|\\.)*"', '', line)
            line = re.sub(r"'(?:[^'\\]|\\.)*'", '', line)
            line = re.sub(r'`(?:[^`\\]|\\.)*`', '', line)

            for char in line:
                if char == '{':
                    brace_count += 1
                    started = True
                elif char == '}':
                    brace_count -= 1

            if started and brace_count == 0:
                return i + 1

        return len(lines)


@ParserRegistry.register
class TypeScriptParser(JavaScriptParser):
    """Parser for TypeScript source code."""

    @property
    def language_id(self) -> str:
        return "typescript"

    @property
    def file_extensions(self) -> list[str]:
        return [".ts", ".tsx"]

    def parse(self, source: str, file_path: Path | None = None) -> ParsedFile:
        """Parse TypeScript source code."""
        result = super().parse(source, file_path)
        result.language = "typescript"
        return result
