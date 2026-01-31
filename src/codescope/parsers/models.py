"""Data models for parsed code structures."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ASTNode:
    """Represents a node in the AST."""

    type: str
    text: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    children: list["ASTNode"] = field(default_factory=list)
    parent: "ASTNode | None" = None

    def find_all(self, node_type: str) -> list["ASTNode"]:
        """Find all descendant nodes of given type."""
        results = []
        if self.type == node_type:
            results.append(self)
        for child in self.children:
            results.extend(child.find_all(node_type))
        return results

    def find_first(self, node_type: str) -> "ASTNode | None":
        """Find first descendant node of given type."""
        if self.type == node_type:
            return self
        for child in self.children:
            result = child.find_first(node_type)
            if result:
                return result
        return None

    def get_text(self) -> str:
        """Get the text content of this node."""
        return self.text


@dataclass
class Import:
    """Represents an import statement."""

    module: str
    names: list[str] = field(default_factory=list)
    alias: str | None = None
    is_from_import: bool = False
    line: int = 0

    def __str__(self) -> str:
        if self.is_from_import:
            names = ", ".join(self.names) if self.names else "*"
            return f"from {self.module} import {names}"
        return f"import {self.module}"


@dataclass
class Parameter:
    """Represents a function parameter."""

    name: str
    type_hint: str | None = None
    default_value: str | None = None
    is_args: bool = False
    is_kwargs: bool = False


@dataclass
class FunctionDef:
    """Represents a function definition."""

    name: str
    start_line: int
    end_line: int
    parameters: list[Parameter] = field(default_factory=list)
    return_type: str | None = None
    decorators: list[str] = field(default_factory=list)
    docstring: str | None = None
    is_method: bool = False
    is_async: bool = False
    body_lines: int = 0
    complexity: int = 1

    @property
    def line_count(self) -> int:
        """Get total line count."""
        return self.end_line - self.start_line + 1


@dataclass
class ClassDef:
    """Represents a class definition."""

    name: str
    start_line: int
    end_line: int
    bases: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    docstring: str | None = None
    methods: list[FunctionDef] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)

    @property
    def line_count(self) -> int:
        """Get total line count."""
        return self.end_line - self.start_line + 1


@dataclass
class Variable:
    """Represents a variable assignment."""

    name: str
    line: int
    type_hint: str | None = None
    value: str | None = None
    scope: str = "module"  # module, class, function


@dataclass
class SymbolTable:
    """Symbol table containing all definitions in a file."""

    imports: list[Import] = field(default_factory=list)
    functions: list[FunctionDef] = field(default_factory=list)
    classes: list[ClassDef] = field(default_factory=list)
    variables: list[Variable] = field(default_factory=list)

    def get_function(self, name: str) -> FunctionDef | None:
        """Get function by name."""
        for func in self.functions:
            if func.name == name:
                return func
        return None

    def get_class(self, name: str) -> ClassDef | None:
        """Get class by name."""
        for cls in self.classes:
            if cls.name == name:
                return cls
        return None

    def get_all_functions(self) -> list[FunctionDef]:
        """Get all functions including methods."""
        all_funcs = list(self.functions)
        for cls in self.classes:
            all_funcs.extend(cls.methods)
        return all_funcs


@dataclass
class ParsedFile:
    """Represents a fully parsed source file."""

    path: Path
    language: str
    source: str
    ast: ASTNode | None = None
    symbols: SymbolTable = field(default_factory=SymbolTable)
    errors: list[str] = field(default_factory=list)

    @property
    def lines(self) -> list[str]:
        """Get source split into lines."""
        return self.source.split("\n")

    @property
    def line_count(self) -> int:
        """Get total line count."""
        return len(self.lines)

    def get_line(self, line_num: int) -> str:
        """Get specific line (1-indexed)."""
        if 1 <= line_num <= len(self.lines):
            return self.lines[line_num - 1]
        return ""

    def get_lines(self, start: int, end: int) -> list[str]:
        """Get range of lines (1-indexed, inclusive)."""
        return self.lines[max(0, start - 1) : end]
