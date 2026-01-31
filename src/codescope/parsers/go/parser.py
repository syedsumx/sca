"""Go parser using regex-based analysis."""

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
class GoParser(LanguageParser):
    """Parser for Go source code."""

    @property
    def language_id(self) -> str:
        return "go"

    @property
    def file_extensions(self) -> list[str]:
        return [".go"]

    def parse(self, source: str, file_path: Path | None = None) -> ParsedFile:
        """Parse Go source code."""
        path = file_path or Path("<string>")
        errors: list[str] = []
        symbols = SymbolTable()

        try:
            symbols = self._extract_symbols(source)
            root_node = self._build_ast(source)
        except Exception as e:
            errors.append(f"Parse error: {str(e)}")
            root_node = ASTNode(type="File", text="", start_line=1, end_line=1)

        return ParsedFile(
            path=path,
            language="go",
            source=source,
            ast=root_node,
            symbols=symbols,
            errors=errors,
        )

    def _build_ast(self, source: str) -> ASTNode:
        """Build a simple AST structure."""
        lines = source.split("\n")
        return ASTNode(
            type="File",
            text=source,
            start_line=1,
            end_line=len(lines),
        )

    def _extract_symbols(self, source: str) -> SymbolTable:
        """Extract symbols from Go source code."""
        symbols = SymbolTable()
        lines = source.split("\n")

        # Package pattern
        package_pattern = r'package\s+(\w+)'

        # Import patterns
        single_import_pattern = r'import\s+"([^"]+)"'
        multi_import_start = r'import\s*\('

        # Function pattern
        func_pattern = r'func\s+(?:\(\s*\w+\s+\*?\w+\s*\)\s+)?(\w+)\s*\(([^)]*)\)(?:\s*\(([^)]*)\)|\s*(\w+(?:\s*,\s*\w+)*))?'

        # Struct pattern (treated as class)
        struct_pattern = r'type\s+(\w+)\s+struct\s*\{'

        # Interface pattern
        interface_pattern = r'type\s+(\w+)\s+interface\s*\{'

        # Variable patterns
        var_pattern = r'(?:var|const)\s+(\w+)\s+'
        short_var_pattern = r'(\w+)\s*:='

        in_import_block = False
        import_block_lines: list[str] = []

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            # Skip comments
            if stripped.startswith("//"):
                continue

            # Handle import blocks
            if re.search(multi_import_start, line):
                in_import_block = True
                continue

            if in_import_block:
                if ")" in line:
                    in_import_block = False
                else:
                    match = re.search(r'"([^"]+)"', line)
                    if match:
                        symbols.imports.append(
                            Import(
                                module=match.group(1),
                                is_from_import=False,
                                line=i,
                            )
                        )
                continue

            # Single import
            for match in re.finditer(single_import_pattern, line):
                symbols.imports.append(
                    Import(
                        module=match.group(1),
                        is_from_import=False,
                        line=i,
                    )
                )

            # Extract functions
            for match in re.finditer(func_pattern, line):
                name = match.group(1)
                params_str = match.group(2)
                returns = match.group(3) or match.group(4) or ""

                params = self._parse_params(params_str)
                end_line = self._find_block_end(lines, i - 1)

                symbols.functions.append(
                    FunctionDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        parameters=params,
                        return_type=returns.strip() if returns else None,
                    )
                )

            # Extract structs (as classes)
            for match in re.finditer(struct_pattern, line):
                name = match.group(1)
                end_line = self._find_block_end(lines, i - 1)

                # Find methods for this struct
                methods = self._find_struct_methods(lines, name)

                symbols.classes.append(
                    ClassDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        methods=methods,
                    )
                )

            # Extract interfaces
            for match in re.finditer(interface_pattern, line):
                name = match.group(1)
                end_line = self._find_block_end(lines, i - 1)

                symbols.classes.append(
                    ClassDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        decorators=["interface"],
                    )
                )

            # Extract variables
            for match in re.finditer(var_pattern, line):
                symbols.variables.append(
                    Variable(
                        name=match.group(1),
                        line=i,
                        scope="package",
                    )
                )

        return symbols

    def _parse_params(self, params_str: str) -> list[Parameter]:
        """Parse Go parameter string."""
        params = []
        if not params_str.strip():
            return params

        # Go parameters can be grouped: (a, b int, c string)
        current_names: list[str] = []

        for part in params_str.split(","):
            part = part.strip()
            if not part:
                continue

            tokens = part.split()
            if len(tokens) >= 2:
                # Last token is type, rest are names
                type_hint = tokens[-1]
                names = tokens[:-1]
                # Add previously accumulated names with this type
                for name in current_names:
                    params.append(Parameter(name=name, type_hint=type_hint))
                current_names = []
                for name in names:
                    params.append(Parameter(name=name, type_hint=type_hint))
            elif len(tokens) == 1:
                # Could be a name waiting for type
                current_names.append(tokens[0])

        return params

    def _find_struct_methods(self, lines: list[str], struct_name: str) -> list[FunctionDef]:
        """Find methods with a receiver of the given struct type."""
        methods = []
        receiver_pattern = rf'func\s+\(\s*\w+\s+\*?{struct_name}\s*\)\s+(\w+)\s*\(([^)]*)\)'

        for i, line in enumerate(lines):
            for match in re.finditer(receiver_pattern, line):
                name = match.group(1)
                params_str = match.group(2)
                params = self._parse_params(params_str)
                end_line = self._find_block_end(lines, i)

                methods.append(
                    FunctionDef(
                        name=name,
                        start_line=i + 1,
                        end_line=end_line,
                        parameters=params,
                        is_method=True,
                    )
                )

        return methods

    def _find_block_end(self, lines: list[str], start_idx: int) -> int:
        """Find the end of a code block by matching braces."""
        brace_count = 0
        started = False

        for i in range(start_idx, len(lines)):
            line = lines[i]
            # Remove strings and comments
            line = re.sub(r'//.*$', '', line)
            line = re.sub(r'"(?:[^"\\]|\\.)*"', '', line)
            line = re.sub(r'`[^`]*`', '', line)

            for char in line:
                if char == '{':
                    brace_count += 1
                    started = True
                elif char == '}':
                    brace_count -= 1

            if started and brace_count == 0:
                return i + 1

        return len(lines)
