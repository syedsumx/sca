"""Data flow analyzer for taint tracking and security analysis."""

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class TaintSource:
    """A source of potentially tainted data."""

    name: str
    source_type: str  # 'user_input', 'file', 'network', 'database', 'environment'
    file_path: str
    line_number: int
    code_snippet: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source_type": self.source_type,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "code_snippet": self.code_snippet,
        }


@dataclass
class TaintSink:
    """A dangerous sink where tainted data should not flow."""

    name: str
    sink_type: str  # 'sql', 'command', 'file', 'network', 'eval', 'html'
    file_path: str
    line_number: int
    code_snippet: str
    vulnerability: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "sink_type": self.sink_type,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "code_snippet": self.code_snippet,
            "vulnerability": self.vulnerability,
        }


@dataclass
class TaintedPath:
    """A path from source to sink through which tainted data flows."""

    source: TaintSource
    sink: TaintSink
    path_variables: list[str]  # Variables through which data flows
    severity: str  # 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW'
    confidence: float  # 0.0 to 1.0

    def to_dict(self) -> dict:
        return {
            "source": self.source.to_dict(),
            "sink": self.sink.to_dict(),
            "path_variables": self.path_variables,
            "severity": self.severity,
            "confidence": self.confidence,
        }


@dataclass
class DataFlowResult:
    """Result of data flow analysis."""

    files_analyzed: int = 0
    sources: list[TaintSource] = field(default_factory=list)
    sinks: list[TaintSink] = field(default_factory=list)
    tainted_paths: list[TaintedPath] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for p in self.tainted_paths if p.severity == 'CRITICAL')

    @property
    def high_count(self) -> int:
        return sum(1 for p in self.tainted_paths if p.severity == 'HIGH')

    def to_dict(self) -> dict:
        return {
            "files_analyzed": self.files_analyzed,
            "sources_count": len(self.sources),
            "sinks_count": len(self.sinks),
            "tainted_paths_count": len(self.tainted_paths),
            "critical_count": self.critical_count,
            "high_count": self.high_count,
            "tainted_paths": [p.to_dict() for p in self.tainted_paths],
        }


class DataFlowAnalyzer:
    """Analyzer for tracking data flow and detecting security issues."""

    # Known taint sources
    SOURCES = {
        # User input
        "input": ("user_input", "User keyboard input"),
        "request.form": ("user_input", "HTTP form data"),
        "request.args": ("user_input", "HTTP query parameters"),
        "request.data": ("user_input", "HTTP request body"),
        "request.json": ("user_input", "HTTP JSON body"),
        "request.cookies": ("user_input", "HTTP cookies"),
        "request.headers": ("user_input", "HTTP headers"),
        "sys.argv": ("user_input", "Command line arguments"),
        "os.environ": ("environment", "Environment variables"),
        "getenv": ("environment", "Environment variable"),

        # File operations
        "open": ("file", "File read"),
        "read": ("file", "File read"),
        "readline": ("file", "File read line"),
        "readlines": ("file", "File read lines"),

        # Network
        "recv": ("network", "Network receive"),
        "recvfrom": ("network", "Network receive from"),
        "urlopen": ("network", "URL fetch"),
        "requests.get": ("network", "HTTP GET request"),
        "requests.post": ("network", "HTTP POST request"),

        # Database
        "fetchone": ("database", "Database fetch one"),
        "fetchall": ("database", "Database fetch all"),
        "fetchmany": ("database", "Database fetch many"),
    }

    # Known dangerous sinks
    SINKS = {
        # SQL injection
        "execute": ("sql", "SQL Injection", "SQL query with user input"),
        "executemany": ("sql", "SQL Injection", "SQL query with user input"),
        "raw": ("sql", "SQL Injection", "Raw SQL query"),

        # Command injection
        "system": ("command", "Command Injection", "OS command execution"),
        "popen": ("command", "Command Injection", "Process open"),
        "Popen": ("command", "Command Injection", "Subprocess"),
        "call": ("command", "Command Injection", "Subprocess call"),
        "run": ("command", "Command Injection", "Subprocess run"),
        "check_output": ("command", "Command Injection", "Subprocess output"),
        "check_call": ("command", "Command Injection", "Subprocess call"),
        "spawn": ("command", "Command Injection", "Process spawn"),

        # Code injection
        "eval": ("eval", "Code Injection", "Dynamic code execution"),
        "exec": ("eval", "Code Injection", "Dynamic code execution"),
        "compile": ("eval", "Code Injection", "Code compilation"),
        "__import__": ("eval", "Code Injection", "Dynamic import"),

        # Path traversal
        "open": ("file", "Path Traversal", "File access with user path"),
        "send_file": ("file", "Path Traversal", "File send"),
        "send_from_directory": ("file", "Path Traversal", "Directory file send"),

        # XSS
        "render_template_string": ("html", "XSS", "Template with user input"),
        "Markup": ("html", "XSS", "HTML markup"),
        "innerHTML": ("html", "XSS", "HTML injection"),

        # SSRF
        "urlopen": ("network", "SSRF", "URL with user input"),
        "requests.get": ("network", "SSRF", "HTTP request with user URL"),
        "requests.post": ("network", "SSRF", "HTTP request with user URL"),
    }

    def __init__(self):
        self.tainted_vars: dict[str, TaintSource] = {}

    def analyze(self, path: Path) -> DataFlowResult:
        """Analyze data flow in a project."""
        result = DataFlowResult()

        if path.is_file():
            sources, sinks, paths = self._analyze_file(path)
            result.sources.extend(sources)
            result.sinks.extend(sinks)
            result.tainted_paths.extend(paths)
            result.files_analyzed = 1
        else:
            for file_path in path.rglob("*.py"):
                if self._should_skip(file_path):
                    continue

                sources, sinks, paths = self._analyze_file(file_path)
                result.sources.extend(sources)
                result.sinks.extend(sinks)
                result.tainted_paths.extend(paths)
                result.files_analyzed += 1

        return result

    def _analyze_file(self, file_path: Path) -> tuple[list[TaintSource], list[TaintSink], list[TaintedPath]]:
        """Analyze a single file."""
        sources: list[TaintSource] = []
        sinks: list[TaintSink] = []
        paths: list[TaintedPath] = []

        try:
            content = file_path.read_text(encoding='utf-8', errors='ignore')
            lines = content.split('\n')
            tree = ast.parse(content)
        except Exception:
            return sources, sinks, paths

        # Reset tainted variables for this file
        self.tainted_vars = {}

        # Find all sources and sinks
        for node in ast.walk(tree):
            # Check for sources
            source = self._check_source(node, str(file_path), lines)
            if source:
                sources.append(source)

                # Track tainted variables
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            self.tainted_vars[target.id] = source

            # Check for sinks
            sink = self._check_sink(node, str(file_path), lines)
            if sink:
                sinks.append(sink)

                # Check if tainted data flows to this sink
                tainted_path = self._check_taint_flow(node, sink)
                if tainted_path:
                    paths.append(tainted_path)

        return sources, sinks, paths

    def _check_source(self, node: ast.AST, file_path: str, lines: list[str]) -> Optional[TaintSource]:
        """Check if node is a taint source."""
        if isinstance(node, ast.Call):
            func_name = self._get_call_name(node)

            for source_pattern, (source_type, desc) in self.SOURCES.items():
                if source_pattern in func_name:
                    snippet = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                    return TaintSource(
                        name=func_name,
                        source_type=source_type,
                        file_path=file_path,
                        line_number=node.lineno,
                        code_snippet=snippet,
                    )

        elif isinstance(node, ast.Subscript):
            # Check for request.form['key'], etc.
            if isinstance(node.value, ast.Attribute):
                full_name = self._get_attr_name(node.value)
                for source_pattern, (source_type, desc) in self.SOURCES.items():
                    if source_pattern in full_name:
                        snippet = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                        return TaintSource(
                            name=full_name,
                            source_type=source_type,
                            file_path=file_path,
                            line_number=node.lineno,
                            code_snippet=snippet,
                        )

        return None

    def _check_sink(self, node: ast.AST, file_path: str, lines: list[str]) -> Optional[TaintSink]:
        """Check if node is a dangerous sink."""
        if isinstance(node, ast.Call):
            func_name = self._get_call_name(node)

            for sink_pattern, (sink_type, vuln, desc) in self.SINKS.items():
                if func_name.endswith(sink_pattern) or sink_pattern == func_name:
                    snippet = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                    return TaintSink(
                        name=func_name,
                        sink_type=sink_type,
                        file_path=file_path,
                        line_number=node.lineno,
                        code_snippet=snippet,
                        vulnerability=vuln,
                    )

        return None

    def _check_taint_flow(self, node: ast.AST, sink: TaintSink) -> Optional[TaintedPath]:
        """Check if tainted data flows to a sink."""
        if not isinstance(node, ast.Call):
            return None

        # Get all variables used in the call
        used_vars = self._get_used_variables(node)

        # Check if any tainted variable flows to this sink
        for var in used_vars:
            if var in self.tainted_vars:
                source = self.tainted_vars[var]

                # Determine severity based on sink type
                severity = "CRITICAL" if sink.sink_type in ["sql", "command", "eval"] else "HIGH"

                return TaintedPath(
                    source=source,
                    sink=sink,
                    path_variables=[var],
                    severity=severity,
                    confidence=0.8,
                )

        # Also check for direct usage of sources in sinks
        for arg in node.args:
            if isinstance(arg, ast.Call):
                func_name = self._get_call_name(arg)
                for source_pattern in self.SOURCES.keys():
                    if source_pattern in func_name:
                        # Direct flow from source to sink
                        source = TaintSource(
                            name=func_name,
                            source_type="direct",
                            file_path=sink.file_path,
                            line_number=sink.line_number,
                            code_snippet=sink.code_snippet,
                        )
                        return TaintedPath(
                            source=source,
                            sink=sink,
                            path_variables=[],
                            severity="CRITICAL",
                            confidence=0.95,
                        )

        return None

    def _get_used_variables(self, node: ast.AST) -> set[str]:
        """Get all variables used in a node."""
        variables = set()

        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                variables.add(child.id)

        return variables

    def _get_call_name(self, node: ast.Call) -> str:
        """Get the full name of a call."""
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            return self._get_attr_name(node.func)
        return ""

    def _get_attr_name(self, node: ast.Attribute) -> str:
        """Get the full attribute name."""
        parts = []

        current = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value

        if isinstance(current, ast.Name):
            parts.append(current.id)

        return ".".join(reversed(parts))

    def _should_skip(self, file_path: Path) -> bool:
        """Check if a file should be skipped."""
        skip_patterns = [
            '__pycache__', '.git', 'node_modules', '.venv', 'venv',
            'test_', '_test.py', 'tests/', 'migrations/',
        ]
        path_str = str(file_path)
        return any(pattern in path_str for pattern in skip_patterns)


def analyze_data_flow(path: Path) -> DataFlowResult:
    """Convenience function to analyze data flow."""
    analyzer = DataFlowAnalyzer()
    return analyzer.analyze(path)
