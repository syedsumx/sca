"""Swift parser using regex-based analysis."""

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
class SwiftParser(LanguageParser):
    """Parser for Swift source code."""

    @property
    def language_id(self) -> str:
        return "swift"

    @property
    def file_extensions(self) -> list[str]:
        return [".swift"]

    def parse(self, source: str, file_path: Path | None = None) -> ParsedFile:
        """Parse Swift source code."""
        path = file_path or Path("<string>")
        errors: list[str] = []
        symbols = SymbolTable()

        try:
            symbols = self._extract_symbols(source)
            root_node = self._build_ast(source)
        except Exception as e:
            errors.append(f"Parse error: {str(e)}")
            root_node = ASTNode(type="SourceFile", text="", start_line=1, end_line=1)

        return ParsedFile(
            path=path,
            language="swift",
            source=source,
            ast=root_node,
            symbols=symbols,
            errors=errors,
        )

    def _build_ast(self, source: str) -> ASTNode:
        """Build a simple AST structure."""
        lines = source.split("\n")
        return ASTNode(
            type="SourceFile",
            text=source,
            start_line=1,
            end_line=len(lines),
        )

    def _extract_symbols(self, source: str) -> SymbolTable:
        """Extract symbols from Swift source code."""
        symbols = SymbolTable()
        lines = source.split("\n")

        # Import pattern
        import_pattern = r'import\s+(\w+(?:\.\w+)*)'

        # Class pattern
        class_pattern = r'(?:public\s+|private\s+|internal\s+|open\s+|final\s+)*class\s+(\w+)(?:<[^>]+>)?(?:\s*:\s*([\w\s,<>]+))?'

        # Struct pattern
        struct_pattern = r'(?:public\s+|private\s+|internal\s+)*struct\s+(\w+)(?:<[^>]+>)?(?:\s*:\s*([\w\s,<>]+))?'

        # Enum pattern
        enum_pattern = r'(?:public\s+|private\s+|internal\s+)*enum\s+(\w+)(?:<[^>]+>)?(?:\s*:\s*([\w\s,<>]+))?'

        # Protocol pattern
        protocol_pattern = r'(?:public\s+|private\s+|internal\s+)*protocol\s+(\w+)(?:\s*:\s*([\w\s,<>]+))?'

        # Function pattern
        func_pattern = r'(?:public\s+|private\s+|internal\s+|open\s+|override\s+|static\s+|class\s+)*func\s+(\w+)\s*(?:<[^>]+>)?\s*\(([^)]*)\)(?:\s*(?:throws\s+)?->\s*([^\{]+))?'

        # Init pattern
        init_pattern = r'(?:public\s+|private\s+|internal\s+|convenience\s+|required\s+)*init\s*(?:\?)?\s*\(([^)]*)\)'

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            # Skip comments
            if stripped.startswith("//"):
                continue

            # Extract imports
            for match in re.finditer(import_pattern, line):
                symbols.imports.append(
                    Import(module=match.group(1), is_from_import=False, line=i)
                )

            # Extract classes
            for match in re.finditer(class_pattern, line):
                name = match.group(1)
                inherits = match.group(2)
                bases = [b.strip() for b in inherits.split(",")] if inherits else []
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

            # Extract structs
            for match in re.finditer(struct_pattern, line):
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
                        decorators=["struct"],
                    )
                )

            # Extract enums
            for match in re.finditer(enum_pattern, line):
                name = match.group(1)
                end_line = self._find_block_end(lines, i - 1)

                symbols.classes.append(
                    ClassDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        decorators=["enum"],
                    )
                )

            # Extract protocols
            for match in re.finditer(protocol_pattern, line):
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
                        decorators=["protocol"],
                    )
                )

            # Extract functions
            for match in re.finditer(func_pattern, line):
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
                    )
                )

        return symbols

    def _extract_methods(self, lines: list[str], start: int, end: int) -> list[FunctionDef]:
        """Extract methods from a class body."""
        methods = []
        func_pattern = r'(?:public\s+|private\s+|internal\s+|open\s+|override\s+|static\s+|class\s+)*func\s+(\w+)\s*(?:<[^>]+>)?\s*\(([^)]*)\)(?:\s*(?:throws\s+)?->\s*([^\{]+))?'

        for i in range(start, min(end, len(lines))):
            line = lines[i]
            for match in re.finditer(func_pattern, line):
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
                    )
                )

        return methods

    def _parse_params(self, params_str: str) -> list[Parameter]:
        """Parse Swift parameter string."""
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
        """Parse a single Swift parameter."""
        # Handle external/internal name: external internal: Type = default
        # Remove default value
        param = param.split("=")[0].strip()

        # Match: label name: Type or name: Type
        match = re.match(r'(?:_\s+)?(\w+)(?:\s+(\w+))?\s*:\s*(.+)', param)
        if match:
            label = match.group(1)
            name = match.group(2) or label
            type_hint = match.group(3).strip()
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
