"""C/C++ parser using regex-based analysis."""

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
class CParser(LanguageParser):
    """Parser for C source code."""

    @property
    def language_id(self) -> str:
        return "c"

    @property
    def file_extensions(self) -> list[str]:
        return [".c", ".h"]

    def parse(self, source: str, file_path: Path | None = None) -> ParsedFile:
        """Parse C source code."""
        path = file_path or Path("<string>")
        errors: list[str] = []
        symbols = SymbolTable()

        try:
            symbols = self._extract_symbols(source)
            root_node = self._build_ast(source)
        except Exception as e:
            errors.append(f"Parse error: {str(e)}")
            root_node = ASTNode(type="TranslationUnit", text="", start_line=1, end_line=1)

        return ParsedFile(
            path=path,
            language="c",
            source=source,
            ast=root_node,
            symbols=symbols,
            errors=errors,
        )

    def _build_ast(self, source: str) -> ASTNode:
        """Build a simple AST structure."""
        lines = source.split("\n")
        return ASTNode(
            type="TranslationUnit",
            text=source,
            start_line=1,
            end_line=len(lines),
        )

    def _extract_symbols(self, source: str) -> SymbolTable:
        """Extract symbols from C source code."""
        symbols = SymbolTable()
        lines = source.split("\n")

        # Include pattern
        include_pattern = r'#include\s*[<"]([^>"]+)[>"]'

        # Function pattern (C style)
        func_pattern = r'^(?:static\s+|extern\s+|inline\s+)*(\w+(?:\s*\*)*)\s+(\w+)\s*\(([^)]*)\)\s*(?:\{|$)'

        # Struct pattern
        struct_pattern = r'(?:typedef\s+)?struct\s+(\w+)?\s*\{'

        # Typedef pattern
        typedef_pattern = r'typedef\s+(?:struct\s+)?(\w+)\s+(\w+)\s*;'

        # Global variable pattern
        var_pattern = r'^(?:static\s+|extern\s+|const\s+)*(\w+(?:\s*\*)*)\s+(\w+)\s*[=;]'

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            # Skip comments
            if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
                continue

            # Extract includes
            for match in re.finditer(include_pattern, line):
                symbols.imports.append(
                    Import(
                        module=match.group(1),
                        is_from_import=False,
                        line=i,
                    )
                )

            # Extract functions
            for match in re.finditer(func_pattern, line):
                return_type = match.group(1).strip()
                name = match.group(2)
                params_str = match.group(3)

                # Skip if not a real function
                if name in ("if", "for", "while", "switch", "return"):
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
                    )
                )

            # Extract structs
            for match in re.finditer(struct_pattern, line):
                name = match.group(1) or f"anonymous_struct_{i}"
                end_line = self._find_block_end(lines, i - 1)

                symbols.classes.append(
                    ClassDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        decorators=["struct"],
                    )
                )

        return symbols

    def _parse_params(self, params_str: str) -> list[Parameter]:
        """Parse C function parameters."""
        params = []
        if not params_str.strip() or params_str.strip() == "void":
            return params

        for param in params_str.split(","):
            param = param.strip()
            if not param:
                continue

            # Handle pointer types
            match = re.match(r'(.+?)\s*(\**)(\w+)(?:\s*\[\s*\d*\s*\])?$', param)
            if match:
                type_hint = match.group(1).strip() + match.group(2)
                name = match.group(3)
                params.append(Parameter(name=name, type_hint=type_hint))
            else:
                # Simple case
                parts = param.split()
                if len(parts) >= 2:
                    params.append(Parameter(
                        name=parts[-1].strip("*"),
                        type_hint=" ".join(parts[:-1])
                    ))

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


@ParserRegistry.register
class CppParser(CParser):
    """Parser for C++ source code."""

    @property
    def language_id(self) -> str:
        return "cpp"

    @property
    def file_extensions(self) -> list[str]:
        return [".cpp", ".hpp", ".cc", ".cxx", ".hxx", ".h++"]

    def parse(self, source: str, file_path: Path | None = None) -> ParsedFile:
        """Parse C++ source code."""
        result = super().parse(source, file_path)
        result.language = "cpp"

        # Additional C++ specific parsing
        self._extract_cpp_features(result, source)

        return result

    def _extract_cpp_features(self, result: ParsedFile, source: str) -> None:
        """Extract C++ specific features like classes, namespaces, templates."""
        lines = source.split("\n")

        # Class pattern
        class_pattern = r'class\s+(\w+)(?:\s*:\s*(?:public|private|protected)\s+(\w+))?'

        # Namespace pattern
        namespace_pattern = r'namespace\s+(\w+)\s*\{'

        for i, line in enumerate(lines, 1):
            # Extract classes
            for match in re.finditer(class_pattern, line):
                name = match.group(1)
                bases = [match.group(2)] if match.group(2) else []
                end_line = self._find_block_end(lines, i - 1)

                # Check if already added as struct
                existing = [c for c in result.symbols.classes if c.name == name]
                if not existing:
                    result.symbols.classes.append(
                        ClassDef(
                            name=name,
                            start_line=i,
                            end_line=end_line,
                            bases=bases,
                        )
                    )
