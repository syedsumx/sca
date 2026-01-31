"""Rust parser using regex-based analysis."""

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
class RustParser(LanguageParser):
    """Parser for Rust source code."""

    @property
    def language_id(self) -> str:
        return "rust"

    @property
    def file_extensions(self) -> list[str]:
        return [".rs"]

    def parse(self, source: str, file_path: Path | None = None) -> ParsedFile:
        """Parse Rust source code."""
        path = file_path or Path("<string>")
        errors: list[str] = []
        symbols = SymbolTable()

        try:
            symbols = self._extract_symbols(source)
            root_node = self._build_ast(source)
        except Exception as e:
            errors.append(f"Parse error: {str(e)}")
            root_node = ASTNode(type="Crate", text="", start_line=1, end_line=1)

        return ParsedFile(
            path=path,
            language="rust",
            source=source,
            ast=root_node,
            symbols=symbols,
            errors=errors,
        )

    def _build_ast(self, source: str) -> ASTNode:
        """Build a simple AST structure."""
        lines = source.split("\n")
        return ASTNode(
            type="Crate",
            text=source,
            start_line=1,
            end_line=len(lines),
        )

    def _extract_symbols(self, source: str) -> SymbolTable:
        """Extract symbols from Rust source code."""
        symbols = SymbolTable()
        lines = source.split("\n")

        # Use patterns
        use_pattern = r'use\s+([\w:]+(?:::\{[^}]+\}|::\*)?)\s*;'

        # Mod pattern
        mod_pattern = r'mod\s+(\w+)\s*[;{]'

        # Function pattern
        fn_pattern = r'(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*(?:<[^>]+>)?\s*\(([^)]*)\)(?:\s*->\s*([^\{]+))?'

        # Struct pattern
        struct_pattern = r'(?:pub\s+)?struct\s+(\w+)(?:<[^>]+>)?(?:\s*\([^)]*\))?(?:\s*where[^{]+)?'

        # Enum pattern
        enum_pattern = r'(?:pub\s+)?enum\s+(\w+)(?:<[^>]+>)?'

        # Trait pattern
        trait_pattern = r'(?:pub\s+)?trait\s+(\w+)(?:<[^>]+>)?(?:\s*:\s*[\w\s+]+)?'

        # Impl pattern
        impl_pattern = r'impl(?:<[^>]+>)?\s+(?:(\w+)\s+for\s+)?(\w+)(?:<[^>]+>)?'

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            # Skip comments
            if stripped.startswith("//"):
                continue

            # Extract use statements
            for match in re.finditer(use_pattern, line):
                symbols.imports.append(
                    Import(module=match.group(1), is_from_import=False, line=i)
                )

            # Extract mods
            for match in re.finditer(mod_pattern, line):
                symbols.imports.append(
                    Import(module=match.group(1), is_from_import=True, line=i)
                )

            # Extract functions
            for match in re.finditer(fn_pattern, line):
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
                        is_async="async" in line,
                    )
                )

            # Extract structs
            for match in re.finditer(struct_pattern, line):
                name = match.group(1)
                end_line = self._find_block_end(lines, i - 1) if "{" in line else i

                symbols.classes.append(
                    ClassDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
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

        return symbols

    def _parse_params(self, params_str: str) -> list[Parameter]:
        """Parse Rust parameter string."""
        params = []
        if not params_str.strip():
            return params

        # Handle self parameter
        if "self" in params_str:
            params_str = re.sub(r'&?\s*mut\s*self\s*,?', '', params_str)
            params_str = re.sub(r'self\s*,?', '', params_str)

        for param in params_str.split(","):
            param = param.strip()
            if not param:
                continue

            # Split name: type
            if ":" in param:
                parts = param.split(":", 1)
                name = parts[0].strip()
                type_hint = parts[1].strip()
                params.append(Parameter(name=name, type_hint=type_hint))
            else:
                params.append(Parameter(name=param))

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

            for char in line:
                if char == '{':
                    brace_count += 1
                    started = True
                elif char == '}':
                    brace_count -= 1

            if started and brace_count == 0:
                return i + 1

        return len(lines)
