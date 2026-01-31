"""C# parser using regex-based analysis."""

import re
from pathlib import Path

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
class CSharpParser(LanguageParser):
    """Parser for C# source code."""

    @property
    def language_id(self) -> str:
        return "csharp"

    @property
    def file_extensions(self) -> list[str]:
        return [".cs"]

    def parse(self, source: str, file_path: Path | None = None) -> ParsedFile:
        """Parse C# source code."""
        path = file_path or Path("<string>")
        errors: list[str] = []
        symbols = SymbolTable()

        try:
            symbols = self._extract_symbols(source)
            root_node = self._build_ast(source)
        except Exception as e:
            errors.append(f"Parse error: {str(e)}")
            root_node = ASTNode(type="CompilationUnit", text="", start_line=1, end_line=1)

        return ParsedFile(
            path=path,
            language="csharp",
            source=source,
            ast=root_node,
            symbols=symbols,
            errors=errors,
        )

    def _build_ast(self, source: str) -> ASTNode:
        """Build a simple AST structure."""
        lines = source.split("\n")
        return ASTNode(
            type="CompilationUnit",
            text=source,
            start_line=1,
            end_line=len(lines),
        )

    def _extract_symbols(self, source: str) -> SymbolTable:
        """Extract symbols from C# source code."""
        symbols = SymbolTable()
        lines = source.split("\n")

        # Using pattern
        using_pattern = r'using\s+(?:static\s+)?([a-zA-Z_][\w.]*)\s*;'

        # Namespace pattern
        namespace_pattern = r'namespace\s+([\w.]+)'

        # Class/Interface/Struct/Enum pattern
        type_pattern = r'(?:public\s+|private\s+|protected\s+|internal\s+)?(?:abstract\s+|sealed\s+|static\s+|partial\s+)*(?:class|interface|struct|enum|record)\s+(\w+)(?:<[^>]+>)?(?:\s*:\s*([\w\s,<>]+))?'

        # Method pattern
        method_pattern = r'(?:public\s+|private\s+|protected\s+|internal\s+)?(?:static\s+|virtual\s+|override\s+|abstract\s+|async\s+)*(\w+(?:<[^>]+>)?(?:\[\])?(?:\?)?)\s+(\w+)\s*\(([^)]*)\)'

        # Property pattern
        property_pattern = r'(?:public\s+|private\s+|protected\s+|internal\s+)?(?:static\s+|virtual\s+|override\s+)?(\w+(?:<[^>]+>)?(?:\[\])?(?:\?)?)\s+(\w+)\s*\{\s*(?:get|set)'

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            # Skip comments
            if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
                continue

            # Extract usings
            for match in re.finditer(using_pattern, line):
                symbols.imports.append(
                    Import(
                        module=match.group(1),
                        is_from_import=False,
                        line=i,
                    )
                )

            # Extract types (classes, interfaces, etc.)
            for match in re.finditer(type_pattern, line):
                name = match.group(1)
                bases_str = match.group(2)
                bases = []
                if bases_str:
                    bases = [b.strip() for b in bases_str.split(",")]

                end_line = self._find_block_end(lines, i - 1)
                methods = self._extract_methods(lines, i, end_line)

                symbols.classes.append(
                    ClassDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        bases=bases,
                        methods=methods,
                    )
                )

            # Extract methods (standalone)
            for match in re.finditer(method_pattern, line):
                return_type = match.group(1)
                name = match.group(2)
                params_str = match.group(3)

                if name in ("if", "for", "while", "switch", "catch", "using", "foreach"):
                    continue

                params = self._parse_params(params_str)
                end_line = self._find_block_end(lines, i - 1)

                symbols.functions.append(
                    FunctionDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        parameters=params,
                        return_type=return_type,
                        is_async="async" in line,
                    )
                )

        return symbols

    def _extract_methods(self, lines: list[str], start: int, end: int) -> list[FunctionDef]:
        """Extract methods from a class body."""
        methods = []
        method_pattern = r'(?:public\s+|private\s+|protected\s+|internal\s+)?(?:static\s+|virtual\s+|override\s+|abstract\s+|async\s+)*(\w+(?:<[^>]+>)?(?:\[\])?(?:\?)?)\s+(\w+)\s*\(([^)]*)\)'

        for i in range(start, min(end, len(lines))):
            line = lines[i]
            for match in re.finditer(method_pattern, line):
                return_type = match.group(1)
                name = match.group(2)
                params_str = match.group(3)

                if name in ("if", "for", "while", "switch", "catch", "using", "foreach", "class"):
                    continue

                params = self._parse_params(params_str)
                method_end = self._find_block_end(lines, i)

                methods.append(
                    FunctionDef(
                        name=name,
                        start_line=i + 1,
                        end_line=method_end,
                        parameters=params,
                        return_type=return_type,
                        is_method=True,
                        is_async="async" in line,
                    )
                )

        return methods

    def _parse_params(self, params_str: str) -> list[Parameter]:
        """Parse C# parameter string."""
        params = []
        if not params_str.strip():
            return params

        for param in params_str.split(","):
            param = param.strip()
            if not param:
                continue

            # Handle 'out', 'ref', 'params', 'in' keywords
            param = re.sub(r'\b(out|ref|params|in)\s+', '', param)
            # Handle attributes
            param = re.sub(r'\[[^\]]+\]\s*', '', param)
            # Handle default values
            param = param.split("=")[0].strip()

            parts = param.split()
            if len(parts) >= 2:
                type_hint = " ".join(parts[:-1])
                name = parts[-1]
                params.append(Parameter(name=name, type_hint=type_hint))
            elif len(parts) == 1:
                params.append(Parameter(name=parts[0]))

        return params

    def _find_block_end(self, lines: list[str], start_idx: int) -> int:
        """Find the end of a code block."""
        brace_count = 0
        started = False

        for i in range(start_idx, len(lines)):
            line = lines[i]
            # Remove strings and comments
            line = re.sub(r'//.*$', '', line)
            line = re.sub(r'"(?:[^"\\]|\\.)*"', '', line)
            line = re.sub(r'@"(?:[^"]|"")*"', '', line)

            for char in line:
                if char == '{':
                    brace_count += 1
                    started = True
                elif char == '}':
                    brace_count -= 1

            if started and brace_count == 0:
                return i + 1

        return len(lines)
