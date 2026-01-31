"""Kotlin parser using regex-based analysis."""

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
class KotlinParser(LanguageParser):
    """Parser for Kotlin source code."""

    @property
    def language_id(self) -> str:
        return "kotlin"

    @property
    def file_extensions(self) -> list[str]:
        return [".kt", ".kts"]

    def parse(self, source: str, file_path: Path | None = None) -> ParsedFile:
        """Parse Kotlin source code."""
        path = file_path or Path("<string>")
        errors: list[str] = []
        symbols = SymbolTable()

        try:
            symbols = self._extract_symbols(source)
            root_node = self._build_ast(source)
        except Exception as e:
            errors.append(f"Parse error: {str(e)}")
            root_node = ASTNode(type="KtFile", text="", start_line=1, end_line=1)

        return ParsedFile(
            path=path,
            language="kotlin",
            source=source,
            ast=root_node,
            symbols=symbols,
            errors=errors,
        )

    def _build_ast(self, source: str) -> ASTNode:
        """Build a simple AST structure."""
        lines = source.split("\n")
        return ASTNode(
            type="KtFile",
            text=source,
            start_line=1,
            end_line=len(lines),
        )

    def _extract_symbols(self, source: str) -> SymbolTable:
        """Extract symbols from Kotlin source code."""
        symbols = SymbolTable()
        lines = source.split("\n")

        # Package pattern
        package_pattern = r'package\s+([\w.]+)'

        # Import pattern
        import_pattern = r'import\s+([\w.*]+)(?:\s+as\s+(\w+))?'

        # Class patterns
        class_pattern = r'(?:public\s+|private\s+|internal\s+|protected\s+)?(?:abstract\s+|open\s+|final\s+|sealed\s+|data\s+|enum\s+)?class\s+(\w+)(?:<[^>]+>)?(?:\s*\([^)]*\))?(?:\s*:\s*([\w\s,<>()]+))?'

        # Interface pattern
        interface_pattern = r'(?:public\s+|private\s+|internal\s+)?interface\s+(\w+)(?:<[^>]+>)?(?:\s*:\s*([\w\s,<>]+))?'

        # Object pattern (singleton)
        object_pattern = r'(?:public\s+|private\s+|internal\s+)?(?:companion\s+)?object\s+(\w+)?(?:\s*:\s*([\w\s,<>()]+))?'

        # Function pattern
        fun_pattern = r'(?:public\s+|private\s+|internal\s+|protected\s+)?(?:open\s+|override\s+|suspend\s+|inline\s+)*fun\s+(?:<[^>]+>\s*)?(\w+)\s*\(([^)]*)\)(?:\s*:\s*([^\{=]+))?'

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            # Skip comments
            if stripped.startswith("//"):
                continue

            # Extract package
            for match in re.finditer(package_pattern, line):
                symbols.imports.append(
                    Import(module=match.group(1), is_from_import=False, line=i)
                )

            # Extract imports
            for match in re.finditer(import_pattern, line):
                alias = match.group(2)
                symbols.imports.append(
                    Import(
                        module=match.group(1),
                        alias=alias,
                        is_from_import=True,
                        line=i,
                    )
                )

            # Extract classes
            for match in re.finditer(class_pattern, line):
                name = match.group(1)
                inherits = match.group(2)
                bases = []
                if inherits:
                    # Parse inheritance list
                    for base in inherits.split(","):
                        base = base.strip()
                        # Remove constructor call parentheses
                        base = re.sub(r'\([^)]*\)', '', base).strip()
                        if base:
                            bases.append(base)

                end_line = self._find_block_end(lines, i - 1)
                methods = self._extract_methods(lines, i, end_line)
                decorators = []
                if "data class" in line:
                    decorators.append("data")
                if "sealed class" in line:
                    decorators.append("sealed")
                if "enum class" in line:
                    decorators.append("enum")

                symbols.classes.append(
                    ClassDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        bases=bases,
                        methods=methods,
                        decorators=decorators,
                    )
                )

            # Extract interfaces
            for match in re.finditer(interface_pattern, line):
                name = match.group(1)
                inherits = match.group(2)
                bases = [b.strip() for b in inherits.split(",")] if inherits else []
                end_line = self._find_block_end(lines, i - 1)

                symbols.classes.append(
                    ClassDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        bases=bases,
                        decorators=["interface"],
                    )
                )

            # Extract objects
            for match in re.finditer(object_pattern, line):
                name = match.group(1) or "Companion"
                inherits = match.group(2)
                bases = []
                if inherits:
                    for base in inherits.split(","):
                        base = re.sub(r'\([^)]*\)', '', base).strip()
                        if base:
                            bases.append(base)

                end_line = self._find_block_end(lines, i - 1)

                symbols.classes.append(
                    ClassDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        bases=bases,
                        decorators=["object"],
                    )
                )

            # Extract functions
            for match in re.finditer(fun_pattern, line):
                name = match.group(1)
                params_str = match.group(2)
                return_type = match.group(3)

                params = self._parse_params(params_str)
                end_line = self._find_block_end(lines, i - 1)

                symbols.functions.append(
                    FunctionDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        parameters=params,
                        return_type=return_type.strip() if return_type else None,
                        is_async="suspend" in line,
                    )
                )

        return symbols

    def _extract_methods(self, lines: list[str], start: int, end: int) -> list[FunctionDef]:
        """Extract methods from a class body."""
        methods = []
        fun_pattern = r'(?:public\s+|private\s+|internal\s+|protected\s+)?(?:open\s+|override\s+|suspend\s+|inline\s+)*fun\s+(?:<[^>]+>\s*)?(\w+)\s*\(([^)]*)\)(?:\s*:\s*([^\{=]+))?'

        for i in range(start, min(end, len(lines))):
            line = lines[i]
            for match in re.finditer(fun_pattern, line):
                name = match.group(1)
                params_str = match.group(2)
                return_type = match.group(3)

                params = self._parse_params(params_str)
                method_end = self._find_block_end(lines, i)

                methods.append(
                    FunctionDef(
                        name=name,
                        start_line=i + 1,
                        end_line=method_end,
                        parameters=params,
                        return_type=return_type.strip() if return_type else None,
                        is_method=True,
                        is_async="suspend" in line,
                    )
                )

        return methods

    def _parse_params(self, params_str: str) -> list[Parameter]:
        """Parse Kotlin parameter string."""
        params = []
        if not params_str.strip():
            return params

        # Split carefully considering nested generics
        depth = 0
        current = ""
        for char in params_str:
            if char in "<(":
                depth += 1
            elif char in ">)":
                depth -= 1
            elif char == "," and depth == 0:
                if current.strip():
                    params.append(self._parse_single_param(current.strip()))
                current = ""
                continue
            current += char

        if current.strip():
            params.append(self._parse_single_param(current.strip()))

        return params

    def _parse_single_param(self, param: str) -> Parameter:
        """Parse a single Kotlin parameter."""
        # Remove default value
        param = param.split("=")[0].strip()
        # Remove vararg/crossinline/noinline
        param = re.sub(r'\b(vararg|crossinline|noinline)\s+', '', param)

        # Match: name: Type
        match = re.match(r'(\w+)\s*:\s*(.+)', param)
        if match:
            name = match.group(1)
            type_hint = match.group(2).strip()
            return Parameter(name=name, type_hint=type_hint)

        return Parameter(name=param)

    def _find_block_end(self, lines: list[str], start_idx: int) -> int:
        """Find the end of a code block."""
        brace_count = 0
        started = False

        for i in range(start_idx, len(lines)):
            line = lines[i]
            # Remove strings and comments
            line = re.sub(r'//.*$', '', line)
            line = re.sub(r'"(?:[^"\\]|\\.)*"', '', line)

            for char in line:
                if char == '{':
                    brace_count += 1
                    started = True
                elif char == '}':
                    brace_count -= 1

            if started and brace_count == 0:
                return i + 1

        return len(lines)
