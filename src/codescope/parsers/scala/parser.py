"""Scala parser using regex-based analysis."""

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
class ScalaParser(LanguageParser):
    """Parser for Scala source code."""

    @property
    def language_id(self) -> str:
        return "scala"

    @property
    def file_extensions(self) -> list[str]:
        return [".scala", ".sc"]

    def parse(self, source: str, file_path: Path | None = None) -> ParsedFile:
        """Parse Scala source code."""
        path = file_path or Path("<string>")
        errors: list[str] = []
        symbols = SymbolTable()

        try:
            symbols = self._extract_symbols(source)
            root_node = self._build_ast(source)
        except Exception as e:
            errors.append(f"Parse error: {str(e)}")
            root_node = ASTNode(type="Source", text="", start_line=1, end_line=1)

        return ParsedFile(
            path=path,
            language="scala",
            source=source,
            ast=root_node,
            symbols=symbols,
            errors=errors,
        )

    def _build_ast(self, source: str) -> ASTNode:
        """Build a simple AST structure."""
        lines = source.split("\n")
        return ASTNode(
            type="Source",
            text=source,
            start_line=1,
            end_line=len(lines),
        )

    def _extract_symbols(self, source: str) -> SymbolTable:
        """Extract symbols from Scala source code."""
        symbols = SymbolTable()
        lines = source.split("\n")

        # Package pattern
        package_pattern = r'package\s+([\w.]+)'

        # Import pattern
        import_pattern = r'import\s+([\w.]+)(?:\.(?:\{([^}]+)\}|\*))?'

        # Class patterns
        class_pattern = r'(?:abstract\s+|final\s+|sealed\s+)?(?:case\s+)?class\s+(\w+)(?:\[[^\]]+\])?(?:\s*\([^)]*\))?(?:\s+extends\s+([\w\s\[\],()]+))?'

        # Object pattern
        object_pattern = r'(?:case\s+)?object\s+(\w+)(?:\s+extends\s+([\w\s\[\],()]+))?'

        # Trait pattern
        trait_pattern = r'(?:sealed\s+)?trait\s+(\w+)(?:\[[^\]]+\])?(?:\s+extends\s+([\w\s\[\],()]+))?'

        # Def pattern (method/function)
        def_pattern = r'(?:override\s+)?(?:private\s*(?:\[\w+\]\s*)?|protected\s*(?:\[\w+\]\s*)?)?def\s+(\w+)(?:\[[^\]]+\])?\s*(?:\(([^)]*)\))*(?:\s*:\s*([^\{=]+))?'

        # Val/Var pattern
        val_pattern = r'(?:private\s*(?:\[\w+\]\s*)?|protected\s*(?:\[\w+\]\s*)?)?(?:lazy\s+)?(val|var)\s+(\w+)(?:\s*:\s*([^\{=]+))?'

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
                module = match.group(1)
                members = match.group(2)
                if members:
                    # Multiple imports from same package
                    for member in members.split(","):
                        member = member.strip()
                        # Handle rename: oldName => newName
                        if "=>" in member:
                            parts = member.split("=>")
                            symbols.imports.append(
                                Import(
                                    module=f"{module}.{parts[0].strip()}",
                                    alias=parts[1].strip(),
                                    is_from_import=True,
                                    line=i,
                                )
                            )
                        else:
                            symbols.imports.append(
                                Import(
                                    module=f"{module}.{member}",
                                    is_from_import=True,
                                    line=i,
                                )
                            )
                else:
                    symbols.imports.append(
                        Import(module=module, is_from_import=True, line=i)
                    )

            # Extract classes
            for match in re.finditer(class_pattern, line):
                name = match.group(1)
                extends = match.group(2)
                bases = self._parse_extends(extends) if extends else []
                end_line = self._find_block_end(lines, i - 1)
                methods = self._extract_methods(lines, i, end_line)

                decorators = []
                if "case class" in line:
                    decorators.append("case")
                if "abstract class" in line:
                    decorators.append("abstract")
                if "sealed class" in line:
                    decorators.append("sealed")

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

            # Extract objects
            for match in re.finditer(object_pattern, line):
                name = match.group(1)
                extends = match.group(2)
                bases = self._parse_extends(extends) if extends else []
                end_line = self._find_block_end(lines, i - 1)

                decorators = ["object"]
                if "case object" in line:
                    decorators.append("case")

                symbols.classes.append(
                    ClassDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        bases=bases,
                        decorators=decorators,
                    )
                )

            # Extract traits
            for match in re.finditer(trait_pattern, line):
                name = match.group(1)
                extends = match.group(2)
                bases = self._parse_extends(extends) if extends else []
                end_line = self._find_block_end(lines, i - 1)

                decorators = ["trait"]
                if "sealed trait" in line:
                    decorators.append("sealed")

                symbols.classes.append(
                    ClassDef(
                        name=name,
                        start_line=i,
                        end_line=end_line,
                        bases=bases,
                        decorators=decorators,
                    )
                )

            # Extract defs (standalone functions)
            for match in re.finditer(def_pattern, line):
                name = match.group(1)
                params_str = match.group(2) or ""
                return_type = match.group(3)

                params = self._parse_params(params_str)
                end_line = self._find_block_end(lines, i - 1) if "{" in line else i

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

    def _parse_extends(self, extends_str: str) -> list[str]:
        """Parse Scala extends clause."""
        bases = []
        # Split by 'with' for mixin traits
        parts = re.split(r'\s+with\s+', extends_str)
        for part in parts:
            # Remove type parameters and constructor args
            base = re.sub(r'\[[^\]]+\]', '', part)
            base = re.sub(r'\([^)]*\)', '', base).strip()
            if base:
                bases.append(base)
        return bases

    def _extract_methods(self, lines: list[str], start: int, end: int) -> list[FunctionDef]:
        """Extract methods from a class body."""
        methods = []
        def_pattern = r'(?:override\s+)?(?:private\s*(?:\[\w+\]\s*)?|protected\s*(?:\[\w+\]\s*)?)?def\s+(\w+)(?:\[[^\]]+\])?\s*(?:\(([^)]*)\))*(?:\s*:\s*([^\{=]+))?'

        for i in range(start, min(end, len(lines))):
            line = lines[i]
            for match in re.finditer(def_pattern, line):
                name = match.group(1)
                params_str = match.group(2) or ""
                return_type = match.group(3)

                params = self._parse_params(params_str)
                method_end = self._find_block_end(lines, i) if "{" in line else i + 1

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
        """Parse Scala parameter string."""
        params = []
        if not params_str.strip():
            return params

        # Split carefully considering nested generics
        depth = 0
        current = ""
        for char in params_str:
            if char in "<[(":
                depth += 1
            elif char in ">])":
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
        """Parse a single Scala parameter."""
        # Remove default value
        param = param.split("=")[0].strip()
        # Remove implicit keyword
        param = re.sub(r'\bimplicit\s+', '', param)

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
