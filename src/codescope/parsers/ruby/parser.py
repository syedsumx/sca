"""Ruby parser using regex-based analysis."""

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
class RubyParser(LanguageParser):
    """Parser for Ruby source code."""

    @property
    def language_id(self) -> str:
        return "ruby"

    @property
    def file_extensions(self) -> list[str]:
        return [".rb", ".rake", ".gemspec"]

    def parse(self, source: str, file_path: Path | None = None) -> ParsedFile:
        """Parse Ruby source code."""
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
            language="ruby",
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
        """Extract symbols from Ruby source code."""
        symbols = SymbolTable()
        lines = source.split("\n")

        # Require patterns
        require_pattern = r'require\s+[\'"]([^\'"]+)[\'"]'
        require_relative_pattern = r'require_relative\s+[\'"]([^\'"]+)[\'"]'

        # Class pattern
        class_pattern = r'class\s+(\w+)(?:\s*<\s*(\w+(?:::\w+)*))?'

        # Module pattern
        module_pattern = r'module\s+(\w+)'

        # Method pattern
        method_pattern = r'def\s+(?:self\.)?(\w+[?!=]?)\s*(?:\(([^)]*)\))?'

        # Attribute accessors
        attr_pattern = r'attr_(?:reader|writer|accessor)\s+(.+)$'

        class_stack: list[str] = []

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            # Skip comments
            if stripped.startswith("#"):
                continue

            # Extract requires
            for match in re.finditer(require_pattern, line):
                symbols.imports.append(
                    Import(module=match.group(1), is_from_import=False, line=i)
                )

            for match in re.finditer(require_relative_pattern, line):
                symbols.imports.append(
                    Import(module=match.group(1), is_from_import=True, line=i)
                )

            # Extract classes
            for match in re.finditer(class_pattern, line):
                name = match.group(1)
                base = match.group(2)
                bases = [base] if base else []
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
                class_stack.append(name)

            # Extract modules (treat as classes)
            for match in re.finditer(module_pattern, line):
                name = match.group(1)
                end_line = self._find_block_end(lines, i - 1)

                symbols.classes.append(
                    ClassDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        decorators=["module"],
                    )
                )

            # Extract standalone methods
            if not class_stack or not any(c in line for c in ["class ", "module "]):
                for match in re.finditer(method_pattern, line):
                    name = match.group(1)
                    params_str = match.group(2) or ""
                    params = self._parse_params(params_str)
                    end_line = self._find_block_end(lines, i - 1)

                    symbols.functions.append(
                        FunctionDef(
                            name=name,
                            start_line=i,
                            end_line=end_line,
                            parameters=params,
                        )
                    )

            # Track end keywords
            if stripped == "end":
                if class_stack:
                    class_stack.pop()

        return symbols

    def _extract_methods(self, lines: list[str], start: int, end: int) -> list[FunctionDef]:
        """Extract methods from a class body."""
        methods = []
        method_pattern = r'def\s+(?:self\.)?(\w+[?!=]?)\s*(?:\(([^)]*)\))?'

        i = start
        while i < min(end, len(lines)):
            line = lines[i]
            for match in re.finditer(method_pattern, line):
                name = match.group(1)
                params_str = match.group(2) or ""
                params = self._parse_params(params_str)
                method_end = self._find_block_end(lines, i)

                methods.append(
                    FunctionDef(
                        name=name,
                        start_line=i + 1,
                        end_line=method_end,
                        parameters=params,
                        is_method=True,
                    )
                )
            i += 1

        return methods

    def _parse_params(self, params_str: str) -> list[Parameter]:
        """Parse Ruby parameter string."""
        params = []
        if not params_str.strip():
            return params

        for param in params_str.split(","):
            param = param.strip()
            if not param:
                continue

            # Handle default values
            name = param.split("=")[0].strip()
            # Handle splat operators
            name = name.lstrip("*&")

            if name:
                params.append(Parameter(name=name))

        return params

    def _find_block_end(self, lines: list[str], start_idx: int) -> int:
        """Find the end of a Ruby block (def...end, class...end, etc.)."""
        block_keywords = {"def", "class", "module", "if", "unless", "case", "while", "until", "for", "begin", "do"}
        end_count = 0
        started = False

        for i in range(start_idx, len(lines)):
            line = lines[i].strip()

            # Skip comments and strings (simplified)
            if line.startswith("#"):
                continue

            # Count block starts
            for keyword in block_keywords:
                if re.search(rf'\b{keyword}\b', line):
                    end_count += 1
                    started = True

            # Count ends
            if re.search(r'\bend\b', line):
                end_count -= 1

            if started and end_count == 0:
                return i + 1

        return len(lines)
