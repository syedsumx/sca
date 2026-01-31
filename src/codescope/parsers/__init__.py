"""Language parsing module for CodeScope."""

from codescope.parsers.base import LanguageParser, ParsedFile
from codescope.parsers.registry import ParserRegistry, get_parser
from codescope.parsers.models import (
    FunctionDef,
    ClassDef,
    Import,
    SymbolTable,
    ASTNode,
)

# Import all language parsers to register them
from codescope.parsers.python import PythonParser
from codescope.parsers.javascript import JavaScriptParser, TypeScriptParser
from codescope.parsers.java import JavaParser
from codescope.parsers.go import GoParser
from codescope.parsers.cpp import CppParser, CParser
from codescope.parsers.csharp import CSharpParser
from codescope.parsers.ruby import RubyParser
from codescope.parsers.php import PHPParser
from codescope.parsers.rust import RustParser
from codescope.parsers.swift import SwiftParser
from codescope.parsers.kotlin import KotlinParser
from codescope.parsers.scala import ScalaParser

__all__ = [
    "LanguageParser",
    "ParsedFile",
    "ParserRegistry",
    "get_parser",
    "FunctionDef",
    "ClassDef",
    "Import",
    "SymbolTable",
    "ASTNode",
    # Language parsers
    "PythonParser",
    "JavaScriptParser",
    "TypeScriptParser",
    "JavaParser",
    "GoParser",
    "CppParser",
    "CParser",
    "CSharpParser",
    "RubyParser",
    "PHPParser",
    "RustParser",
    "SwiftParser",
    "KotlinParser",
    "ScalaParser",
]
