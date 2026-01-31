"""Custom exceptions for CodeScope."""


class CodeScopeError(Exception):
    """Base exception for all CodeScope errors."""

    pass


class ConfigurationError(CodeScopeError):
    """Error in configuration."""

    pass


class ParserError(CodeScopeError):
    """Error during code parsing."""

    def __init__(self, message: str, file_path: str | None = None):
        self.file_path = file_path
        super().__init__(f"{file_path}: {message}" if file_path else message)


class AnalysisError(CodeScopeError):
    """Error during analysis."""

    def __init__(self, message: str, rule_id: str | None = None):
        self.rule_id = rule_id
        super().__init__(f"[{rule_id}] {message}" if rule_id else message)


class RuleError(CodeScopeError):
    """Error in rule definition or execution."""

    def __init__(self, message: str, rule_id: str):
        self.rule_id = rule_id
        super().__init__(f"Rule {rule_id}: {message}")


class QualityGateError(CodeScopeError):
    """Error in quality gate evaluation."""

    pass


class ReporterError(CodeScopeError):
    """Error during report generation."""

    pass
