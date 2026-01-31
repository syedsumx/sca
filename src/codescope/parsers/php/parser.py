"""PHP parser using regex-based analysis."""

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
class PHPParser(LanguageParser):
    """Parser for PHP source code."""

    @property
    def language_id(self) -> str:
        return "php"

    @property
    def file_extensions(self) -> list[str]:
        return [".php", ".phtml", ".php5", ".php7"]

    def parse(self, source: str, file_path: Path | None = None) -> ParsedFile:
        """Parse PHP source code."""
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
            language="php",
            source=source,
            ast=root_node,
            symbols=symbols,
            errors=errors,
        )

    def _build_ast(self, source: str) -> ASTNode:
        """Build a simple AST structure."""
        lines = source.split("\n")
        return ASTNode(
            type="Program",
            text=source,
            start_line=1,
            end_line=len(lines),
        )

    def _extract_symbols(self, source: str) -> SymbolTable:
        """Extract symbols from PHP source code."""
        symbols = SymbolTable()
        lines = source.split("\n")

        # Use/namespace patterns
        use_pattern = r'use\s+([\w\\]+)(?:\s+as\s+(\w+))?;'
        namespace_pattern = r'namespace\s+([\w\\]+);'

        # Class/Interface/Trait patterns
        class_pattern = r'(?:abstract\s+|final\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?(?:\s+implements\s+([\w,\s\\]+))?'
        interface_pattern = r'interface\s+(\w+)(?:\s+extends\s+([\w,\s\\]+))?'
        trait_pattern = r'trait\s+(\w+)'

        # Function/Method patterns
        function_pattern = r'(?:public\s+|private\s+|protected\s+)?(?:static\s+)?function\s+(\w+)\s*\(([^)]*)\)(?:\s*:\s*\??([\w\\]+))?'

        # Variable pattern (class properties)
        property_pattern = r'(?:public|private|protected)\s+(?:static\s+)?(?:\??\w+\s+)?\$(\w+)'

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            # Skip comments
            if stripped.startswith("//") or stripped.startswith("#") or stripped.startswith("/*") or stripped.startswith("*"):
                continue

            # Extract namespaces
            for match in re.finditer(namespace_pattern, line):
                symbols.imports.append(
                    Import(module=match.group(1), is_from_import=False, line=i)
                )

            # Extract use statements
            for match in re.finditer(use_pattern, line):
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
                extends = match.group(2)
                implements = match.group(3)

                bases = []
                if extends:
                    bases.append(extends)
                if implements:
                    bases.extend([b.strip() for b in implements.split(",")])

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

            # Extract interfaces
            for match in re.finditer(interface_pattern, line):
                name = match.group(1)
                extends = match.group(2)
                bases = [b.strip() for b in extends.split(",")] if extends else []
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

            # Extract traits
            for match in re.finditer(trait_pattern, line):
                name = match.group(1)
                end_line = self._find_block_end(lines, i - 1)

                symbols.classes.append(
                    ClassDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        decorators=["trait"],
                    )
                )

            # Extract standalone functions
            for match in re.finditer(function_pattern, line):
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
                        return_type=return_type,
                    )
                )

        return symbols

    def _extract_methods(self, lines: list[str], start: int, end: int) -> list[FunctionDef]:
        """Extract methods from a class body."""
        methods = []
        function_pattern = r'(?:public\s+|private\s+|protected\s+)?(?:static\s+)?function\s+(\w+)\s*\(([^)]*)\)(?:\s*:\s*\??([\w\\]+))?'

        for i in range(start, min(end, len(lines))):
            line = lines[i]
            for match in re.finditer(function_pattern, line):
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
                        return_type=return_type,
                        is_method=True,
                    )
                )

        return methods

    def _parse_params(self, params_str: str) -> list[Parameter]:
        """Parse PHP parameter string."""
        params = []
        if not params_str.strip():
            return params

        for param in params_str.split(","):
            param = param.strip()
            if not param:
                continue

            # Handle default values
            param = param.split("=")[0].strip()

            # Extract type hint and name
            parts = param.split()
            if len(parts) >= 2:
                type_hint = parts[0].lstrip("?")
                name = parts[-1].lstrip("$&")
                params.append(Parameter(name=name, type_hint=type_hint))
            elif len(parts) == 1:
                name = parts[0].lstrip("$&")
                params.append(Parameter(name=name))

        return params

    def _find_block_end(self, lines: list[str], start_idx: int) -> int:
        """Find the end of a code block."""
        brace_count = 0
        started = False

        for i in range(start_idx, len(lines)):
            line = lines[i]
            # Remove strings and comments
            line = re.sub(r'//.*$', '', line)
            line = re.sub(r'#.*$', '', line)
            line = re.sub(r'"(?:[^"\\]|\\.)*"', '', line)
            line = re.sub(r"'(?:[^'\\]|\\.)*'", '', line)

            for char in line:
                if char == '{':
                    brace_count += 1
                    started = True
                elif char == '}':
                    brace_count -= 1

            if started and brace_count == 0:
                return i + 1

        return len(lines)
