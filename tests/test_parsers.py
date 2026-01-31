"""Tests for the parser module."""

import pytest
from pathlib import Path

from codescope.parsers import get_parser, ParserRegistry
from codescope.parsers.python.parser import PythonParser


class TestParserRegistry:
    """Tests for ParserRegistry."""

    def test_get_python_parser(self):
        """Test getting Python parser by language."""
        parser = ParserRegistry.get_parser("python")
        assert parser is not None
        assert isinstance(parser, PythonParser)

    def test_get_parser_for_file(self):
        """Test getting parser by file extension."""
        parser = ParserRegistry.get_parser_for_file(Path("test.py"))
        assert parser is not None
        assert parser.language_id == "python"

    def test_unknown_language(self):
        """Test getting parser for unknown language."""
        parser = ParserRegistry.get_parser("unknown")
        assert parser is None

    def test_supported_languages(self):
        """Test listing supported languages."""
        languages = ParserRegistry.get_supported_languages()
        assert "python" in languages


class TestPythonParser:
    """Tests for PythonParser."""

    def test_parse_simple_function(self):
        """Test parsing a simple function."""
        code = '''
def hello():
    return "Hello, World!"
'''
        parser = PythonParser()
        result = parser.parse(code)

        assert result.language == "python"
        assert len(result.symbols.functions) == 1
        assert result.symbols.functions[0].name == "hello"

    def test_parse_class(self):
        """Test parsing a class definition."""
        code = '''
class MyClass:
    def __init__(self):
        self.value = 0

    def get_value(self):
        return self.value
'''
        parser = PythonParser()
        result = parser.parse(code)

        assert len(result.symbols.classes) == 1
        assert result.symbols.classes[0].name == "MyClass"
        assert len(result.symbols.classes[0].methods) == 2

    def test_parse_imports(self):
        """Test parsing import statements."""
        code = '''
import os
from pathlib import Path
import json as j
'''
        parser = PythonParser()
        result = parser.parse(code)

        assert len(result.symbols.imports) == 3

    def test_parse_syntax_error(self):
        """Test handling syntax errors."""
        code = '''
def broken(
    # Missing closing parenthesis
'''
        parser = PythonParser()
        result = parser.parse(code)

        assert len(result.errors) > 0

    def test_function_complexity(self):
        """Test that function complexity is calculated."""
        code = '''
def complex_func(x):
    if x > 0:
        for i in range(x):
            if i % 2 == 0:
                print(i)
    return x
'''
        parser = PythonParser()
        result = parser.parse(code)

        func = result.symbols.functions[0]
        assert func.complexity > 1
