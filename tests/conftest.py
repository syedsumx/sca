"""Shared test fixtures for CodeScope tests."""

import pytest
from pathlib import Path


@pytest.fixture
def sample_python_code():
    """Sample Python code with various issues."""
    return '''
import os
import subprocess

# Hardcoded password (security issue)
password = "<your_password>"
api_key = "<your_api_key>"

def process_user_input(user_input):
    """Function with SQL injection vulnerability."""
    query = "SELECT * FROM users WHERE name = '%s'" % user_input
    return query

def execute_command(command):
    """Function with command injection vulnerability."""
    os.system(command)
    subprocess.call(command, shell=True)

def complex_function(a, b, c, d, e, f, g, h, i):
    """Function with too many parameters and high complexity."""
    result = 0
    if a > 0:
        if b > 0:
            if c > 0:
                if d > 0:
                    if e > 0:
                        result = a + b + c + d + e
                    else:
                        result = a + b + c + d
                else:
                    result = a + b + c
            else:
                result = a + b
        else:
            result = a
    return result

def unused_variables():
    """Function with unused variables."""
    x = 10
    y = 20
    z = 30
    return x  # y and z are unused

def mutable_default(items=[]):
    """Function with mutable default argument."""
    items.append(1)
    return items

def compare_to_none(x):
    """Function comparing to None incorrectly."""
    if x == None:
        return True
    return False

try:
    risky_operation()
except:
    pass  # Bare except with pass
'''


@pytest.fixture
def clean_python_code():
    """Clean Python code without issues."""
    return '''
"""A clean Python module."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def calculate_sum(a: int, b: int) -> int:
    """Calculate the sum of two numbers."""
    return a + b


def greet(name: Optional[str] = None) -> str:
    """Generate a greeting message."""
    if name is None:
        return "Hello, World!"
    return f"Hello, {name}!"


class Calculator:
    """A simple calculator class."""

    def add(self, a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    def subtract(self, a: int, b: int) -> int:
        """Subtract b from a."""
        return a - b
'''


@pytest.fixture
def temp_project(tmp_path):
    """Create a temporary project directory with sample files."""
    # Create project structure
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    # Create a sample Python file
    (src_dir / "main.py").write_text('''
def main():
    print("Hello, World!")

if __name__ == "__main__":
    main()
''')

    # Create a file with issues
    (src_dir / "vulnerable.py").write_text('''
import os

password = "<your_password>"

def run_command(cmd):
    os.system(cmd)
''')

    return tmp_path
