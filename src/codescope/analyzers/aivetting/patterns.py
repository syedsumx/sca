"""Pattern definitions for detecting AI-generated code issues."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PatternCategory(str, Enum):
    """Categories of AI-generated code issues."""

    PLACEHOLDER = "placeholder"
    HALLUCINATION = "hallucination"
    SECURITY = "security"
    QUALITY = "quality"
    INCOMPLETE = "incomplete"
    OVERENGINEERED = "overengineered"
    LICENSE_RISK = "license_risk"


class PatternSeverity(str, Enum):
    BLOCKER = "BLOCKER"
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"
    INFO = "INFO"


@dataclass
class AICodePattern:
    """A pattern that indicates an AI-generated code issue."""

    pattern_id: str
    name: str
    description: str
    category: PatternCategory
    severity: PatternSeverity
    regex: str
    message_template: str
    languages: list[str] = field(default_factory=lambda: ["all"])
    confidence: float = 0.8
    # If True, match against entire file content; otherwise per-line
    multiline: bool = False


# ─── Placeholder & Stub Patterns ────────────────────────────────────────────

_PLACEHOLDER_PATTERNS = [
    AICodePattern(
        pattern_id="ai:placeholder-comment",
        name="Placeholder Comment",
        description="AI models often leave TODO/FIXME/placeholder comments indicating incomplete implementation.",
        category=PatternCategory.PLACEHOLDER,
        severity=PatternSeverity.MAJOR,
        regex=r"#\s*(?:TODO|FIXME|HACK|XXX)\s*:?\s*(?:implement|add|replace|fill|complete|finish|your|actual|put\s+your|insert|change\s+this)",
        message_template="Placeholder comment suggests incomplete AI-generated code: '{match}'",
        languages=["python"],
        confidence=0.85,
    ),
    AICodePattern(
        pattern_id="ai:placeholder-comment-js",
        name="Placeholder Comment (JS/TS)",
        description="AI models often leave TODO/FIXME/placeholder comments indicating incomplete implementation.",
        category=PatternCategory.PLACEHOLDER,
        severity=PatternSeverity.MAJOR,
        regex=r"//\s*(?:TODO|FIXME|HACK|XXX)\s*:?\s*(?:implement|add|replace|fill|complete|finish|your|actual|put\s+your|insert|change\s+this)",
        message_template="Placeholder comment suggests incomplete AI-generated code: '{match}'",
        languages=["javascript", "typescript", "java", "go", "c", "cpp", "csharp"],
        confidence=0.85,
    ),
    AICodePattern(
        pattern_id="ai:stub-pass",
        name="Stub Function Body",
        description="Function body contains only 'pass' or 'raise NotImplementedError', a common AI code stub.",
        category=PatternCategory.PLACEHOLDER,
        severity=PatternSeverity.MAJOR,
        regex=r"def\s+\w+\(.*?\).*?:\s*\n\s+(?:pass|raise\s+NotImplementedError|\.\.\.)\s*$",
        message_template="Stub function body — AI may have left this unimplemented",
        languages=["python"],
        confidence=0.7,
        multiline=True,
    ),
    AICodePattern(
        pattern_id="ai:example-value",
        name="Example / Dummy Value",
        description="Hardcoded example values commonly left by AI (e.g., example.com, John Doe, 12345).",
        category=PatternCategory.PLACEHOLDER,
        severity=PatternSeverity.MAJOR,
        regex=r"""(?:["'])(?:example\.com|johndoe|john\.doe|test@example|user@example|foo@bar|sample_?api_?key|sk-[a-zA-Z0-9]{10,}|your[_-]?api[_-]?key|replace[_-]?me|changeme|dummy[_-]?(?:data|value|token|key|secret)|placeholder|lorem\s+ipsum)(?:["'])""",
        message_template="Possible example/dummy value left by AI: '{match}'",
        languages=["all"],
        confidence=0.8,
    ),
]

# ─── Hallucination Patterns ─────────────────────────────────────────────────

_HALLUCINATION_PATTERNS = [
    AICodePattern(
        pattern_id="ai:hallucinated-import-py",
        name="Potentially Hallucinated Import",
        description="Import of a module that is commonly hallucinated by AI models.",
        category=PatternCategory.HALLUCINATION,
        severity=PatternSeverity.CRITICAL,
        regex=r"(?:from|import)\s+(?:utils\.helpers|helpers\.utils|common\.utils\.helpers|app\.core\.helpers|src\.utils|lib\.helpers|services\.helper)",
        message_template="Potentially hallucinated import — verify this module exists: '{match}'",
        languages=["python"],
        confidence=0.75,
    ),
    AICodePattern(
        pattern_id="ai:hallucinated-api",
        name="Non-Existent API Call",
        description="Call to an API method that doesn't exist on the object — a common LLM hallucination.",
        category=PatternCategory.HALLUCINATION,
        severity=PatternSeverity.CRITICAL,
        regex=r"(?:os\.path\.exists_or_create|os\.makedirs_p|json\.loads_safe|str\.to_int|list\.flat_map|dict\.merge|pathlib\.Path\.makedirs|requests\.fetch|urllib\.download)",
        message_template="Likely hallucinated API call — this method does not exist: '{match}'",
        languages=["python"],
        confidence=0.95,
    ),
    AICodePattern(
        pattern_id="ai:hallucinated-api-js",
        name="Non-Existent JS API Call",
        description="Call to a JavaScript API method that doesn't exist.",
        category=PatternCategory.HALLUCINATION,
        severity=PatternSeverity.CRITICAL,
        regex=r"(?:Array\.flatten|Object\.merge|String\.contains|Array\.unique|Promise\.delay|console\.success|document\.queryAll|window\.navigate|Math\.clamp)",
        message_template="Likely hallucinated API call — this method does not exist: '{match}'",
        languages=["javascript", "typescript"],
        confidence=0.95,
    ),
    AICodePattern(
        pattern_id="ai:phantom-config",
        name="Phantom Configuration Key",
        description="References to configuration keys or environment variables that may be fabricated by AI.",
        category=PatternCategory.HALLUCINATION,
        severity=PatternSeverity.MAJOR,
        regex=r"""os\.(?:environ|getenv)\s*[\[\(]\s*["'](?:APP_SECRET_KEY|DATABASE_SECRET|JWT_PRIVATE_KEY|OPENAI_SECRET|MY_API_TOKEN|SERVICE_AUTH_KEY|MASTER_PASSWORD)["']""",
        message_template="Possibly fabricated environment variable — verify this is defined: '{match}'",
        languages=["python"],
        confidence=0.7,
    ),
]

# ─── Security Patterns ──────────────────────────────────────────────────────

_SECURITY_PATTERNS = [
    AICodePattern(
        pattern_id="ai:hardcoded-secret",
        name="Hardcoded Secret or Key",
        description="AI models frequently embed example secrets, API keys, or tokens directly in code.",
        category=PatternCategory.SECURITY,
        severity=PatternSeverity.BLOCKER,
        regex=r"""(?:api[_-]?key|secret[_-]?key|auth[_-]?token|password|private[_-]?key|access[_-]?token)\s*[=:]\s*["'][A-Za-z0-9+/=_\-]{16,}["']""",
        message_template="Hardcoded secret detected — AI-generated code often embeds example credentials: '{match}'",
        languages=["all"],
        confidence=0.9,
    ),
    AICodePattern(
        pattern_id="ai:insecure-default",
        name="Insecure Default Configuration",
        description="AI often generates code with insecure defaults like debug=True, CORS allow-all, or disabled auth.",
        category=PatternCategory.SECURITY,
        severity=PatternSeverity.CRITICAL,
        regex=r"""(?:DEBUG\s*=\s*True|allow_origins\s*=\s*\["?\*"?\]|verify\s*=\s*False|check_hostname\s*=\s*False|CORS_ALLOW_ALL|disable_auth|auth_required\s*=\s*False|SSL_VERIFY\s*=\s*False)""",
        message_template="Insecure default configuration — AI-generated code often ships unsafe defaults: '{match}'",
        languages=["all"],
        confidence=0.85,
    ),
    AICodePattern(
        pattern_id="ai:eval-exec",
        name="Dynamic Code Execution",
        description="AI may generate code using eval()/exec() which is almost always dangerous.",
        category=PatternCategory.SECURITY,
        severity=PatternSeverity.BLOCKER,
        regex=r"\b(?:eval|exec)\s*\(",
        message_template="Dynamic code execution via eval/exec — review carefully if AI-generated: '{match}'",
        languages=["python"],
        confidence=0.9,
    ),
    AICodePattern(
        pattern_id="ai:sql-string-format",
        name="SQL String Formatting",
        description="AI models frequently generate SQL queries using string formatting instead of parameterized queries.",
        category=PatternCategory.SECURITY,
        severity=PatternSeverity.BLOCKER,
        regex=r"""(?:execute|cursor\.execute|query)\s*\(\s*f?["'](?:SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER)\b.*?(?:\{|\%s|\%\()""",
        message_template="SQL query built via string formatting — use parameterized queries instead: '{match}'",
        languages=["python"],
        confidence=0.9,
    ),
    AICodePattern(
        pattern_id="ai:shell-injection",
        name="Shell Command String Building",
        description="AI may build shell commands via string concatenation or f-strings instead of using list arguments.",
        category=PatternCategory.SECURITY,
        severity=PatternSeverity.BLOCKER,
        regex=r"""subprocess\.(?:run|call|Popen|check_output|check_call)\s*\(\s*f?["']""",
        message_template="Shell command built as string — use list form to prevent injection: '{match}'",
        languages=["python"],
        confidence=0.9,
    ),
    AICodePattern(
        pattern_id="ai:weak-crypto",
        name="Weak Cryptography",
        description="AI often generates code using MD5 or SHA1 for security purposes.",
        category=PatternCategory.SECURITY,
        severity=PatternSeverity.CRITICAL,
        regex=r"""hashlib\.(?:md5|sha1)\s*\(""",
        message_template="Weak hash algorithm — use SHA-256+ for security purposes: '{match}'",
        languages=["python"],
        confidence=0.85,
    ),
]

# ─── Quality Patterns ───────────────────────────────────────────────────────

_QUALITY_PATTERNS = [
    AICodePattern(
        pattern_id="ai:bare-except",
        name="Bare Except Clause",
        description="AI frequently generates broad exception handlers that swallow errors.",
        category=PatternCategory.QUALITY,
        severity=PatternSeverity.MAJOR,
        regex=r"except\s*:\s*$|except\s+Exception\s*:\s*\n\s*pass",
        message_template="Overly broad exception handling — common AI pattern that hides bugs",
        languages=["python"],
        confidence=0.8,
    ),
    AICodePattern(
        pattern_id="ai:catch-all-js",
        name="Empty Catch Block",
        description="AI frequently generates try/catch blocks with empty catch bodies.",
        category=PatternCategory.QUALITY,
        severity=PatternSeverity.MAJOR,
        regex=r"catch\s*\(\w*\)\s*\{\s*\}",
        message_template="Empty catch block — common AI pattern that silently swallows errors",
        languages=["javascript", "typescript", "java"],
        confidence=0.8,
    ),
    AICodePattern(
        pattern_id="ai:redundant-type-conversion",
        name="Redundant Type Conversion",
        description="AI may apply unnecessary type conversions like str(string_var) or int(integer_var).",
        category=PatternCategory.QUALITY,
        severity=PatternSeverity.MINOR,
        regex=r"\bstr\s*\(\s*['\"].*?['\"]\s*\)|\bint\s*\(\s*\d+\s*\)|\bfloat\s*\(\s*\d+\.\d+\s*\)|\bbool\s*\(\s*(?:True|False)\s*\)",
        message_template="Redundant type conversion — value is already this type: '{match}'",
        languages=["python"],
        confidence=0.9,
    ),
    AICodePattern(
        pattern_id="ai:verbose-boolean",
        name="Verbose Boolean Logic",
        description="AI tends to write 'if x == True' or 'if x == False' instead of 'if x' / 'if not x'.",
        category=PatternCategory.QUALITY,
        severity=PatternSeverity.MINOR,
        regex=r"if\s+\w+\s*==\s*(?:True|False)\b|if\s+\w+\s*!=\s*(?:True|False)\b|if\s+\w+\s*is\s+(?:True|False)\b",
        message_template="Verbose boolean comparison — use 'if x' or 'if not x' instead: '{match}'",
        languages=["python"],
        confidence=0.85,
    ),
    AICodePattern(
        pattern_id="ai:print-debug",
        name="Print Statement Debugging",
        description="AI-generated code often includes print() statements for debugging that should use logging.",
        category=PatternCategory.QUALITY,
        severity=PatternSeverity.MINOR,
        regex=r"""print\s*\(\s*f?["'](?:DEBUG|Error|Warning|INFO|LOG|>>>|---|\*\*\*)""",
        message_template="Debug print statement — use proper logging instead: '{match}'",
        languages=["python"],
        confidence=0.8,
    ),
]

# ─── Incomplete Code Patterns ───────────────────────────────────────────────

_INCOMPLETE_PATTERNS = [
    AICodePattern(
        pattern_id="ai:truncated-code",
        name="Truncated Code Block",
        description="AI output may be cut off mid-statement due to token limits.",
        category=PatternCategory.INCOMPLETE,
        severity=PatternSeverity.BLOCKER,
        regex=r"#\s*\.\.\.\s*(?:rest|more|remaining|continued|truncated|etc)\b|#\s*(?:and so on|add more|similar for)",
        message_template="Possibly truncated AI output — code may be incomplete: '{match}'",
        languages=["all"],
        confidence=0.9,
    ),
    AICodePattern(
        pattern_id="ai:ellipsis-code",
        name="Ellipsis Placeholder",
        description="AI uses '...' as a placeholder for code it didn't generate.",
        category=PatternCategory.INCOMPLETE,
        severity=PatternSeverity.CRITICAL,
        regex=r"^\s*\.\.\.\s*$",
        message_template="Ellipsis placeholder — AI left this section unimplemented",
        languages=["python"],
        confidence=0.7,
    ),
    AICodePattern(
        pattern_id="ai:comment-block-pseudo",
        name="Pseudocode in Comments",
        description="AI sometimes leaves pseudocode or natural language instructions as comments instead of real code.",
        category=PatternCategory.INCOMPLETE,
        severity=PatternSeverity.MAJOR,
        regex=r"#\s*(?:Step\s+\d|First,?\s+|Then,?\s+|Next,?\s+|Finally,?\s+|Here\s+we\s+|This\s+(?:function|method|class)\s+(?:should|will|would|needs?\s+to))",
        message_template="Pseudocode comment — AI may not have generated the actual implementation: '{match}'",
        languages=["python"],
        confidence=0.7,
    ),
    AICodePattern(
        pattern_id="ai:comment-block-pseudo-js",
        name="Pseudocode in Comments (JS/TS)",
        description="AI sometimes leaves pseudocode or natural language instructions as comments instead of real code.",
        category=PatternCategory.INCOMPLETE,
        severity=PatternSeverity.MAJOR,
        regex=r"//\s*(?:Step\s+\d|First,?\s+|Then,?\s+|Next,?\s+|Finally,?\s+|Here\s+we\s+|This\s+(?:function|method|class)\s+(?:should|will|would|needs?\s+to))",
        message_template="Pseudocode comment — AI may not have generated the actual implementation: '{match}'",
        languages=["javascript", "typescript", "java", "go", "cpp", "csharp"],
        confidence=0.7,
    ),
]

# ─── Over-Engineering Patterns ──────────────────────────────────────────────

_OVERENGINEERED_PATTERNS = [
    AICodePattern(
        pattern_id="ai:unnecessary-abstract",
        name="Unnecessary Abstraction Layer",
        description="AI tends to create abstract base classes and factory patterns when a simple function suffices.",
        category=PatternCategory.OVERENGINEERED,
        severity=PatternSeverity.MINOR,
        regex=r"class\s+\w*(?:Factory|Builder|Singleton|Proxy|Adapter|Facade|Strategy|Observer)\b.*?(?:ABC|abc\.ABC|abstractmethod|Abstract\w+)",
        message_template="Potentially unnecessary design pattern — verify this abstraction is needed: '{match}'",
        languages=["python"],
        confidence=0.5,
    ),
    AICodePattern(
        pattern_id="ai:excessive-docstring",
        name="Excessive Boilerplate Docstring",
        description="AI often generates verbose docstrings with redundant parameter descriptions.",
        category=PatternCategory.OVERENGINEERED,
        severity=PatternSeverity.INFO,
        regex=r'"""(?:\w+\s+){1,3}(?:the|a|an)\s+\w+\.?\s*\n\s*\n\s*(?:Args|Parameters|Params)\s*:\s*\n(?:\s+\w+\s*(?:\(.*?\))?\s*:\s*.*\n){6,}',
        message_template="Verbose docstring with 6+ parameters — consider simplifying the function signature",
        languages=["python"],
        confidence=0.6,
        multiline=True,
    ),
]

# ─── License Risk Patterns ──────────────────────────────────────────────────

_LICENSE_RISK_PATTERNS = [
    AICodePattern(
        pattern_id="ai:copied-license-header",
        name="Copied License Header",
        description="AI may copy code verbatim from open-source projects, including their license headers.",
        category=PatternCategory.LICENSE_RISK,
        severity=PatternSeverity.CRITICAL,
        regex=r"(?:#|//|/\*)\s*(?:Copyright\s+(?:\(c\)\s*)?\d{4}|Licensed\s+under\s+the\s+(?:MIT|Apache|GPL|BSD|Mozilla|LGPL))",
        message_template="License header detected — AI may have copied code from an open-source project: '{match}'",
        languages=["all"],
        confidence=0.7,
    ),
    AICodePattern(
        pattern_id="ai:stackoverflow-marker",
        name="StackOverflow Attribution",
        description="AI training data includes StackOverflow answers. Code with SO markers may have CC BY-SA license obligations.",
        category=PatternCategory.LICENSE_RISK,
        severity=PatternSeverity.MAJOR,
        regex=r"(?:#|//)\s*(?:Source|From|Adapted from|Based on|Credit|Reference)\s*:?\s*(?:https?://)?(?:stackoverflow\.com|github\.com|gist\.github)",
        message_template="Code attributed to external source — check license compatibility: '{match}'",
        languages=["all"],
        confidence=0.9,
    ),
]

# ─── All built-in patterns ──────────────────────────────────────────────────

BUILTIN_PATTERNS: list[AICodePattern] = (
    _PLACEHOLDER_PATTERNS
    + _HALLUCINATION_PATTERNS
    + _SECURITY_PATTERNS
    + _QUALITY_PATTERNS
    + _INCOMPLETE_PATTERNS
    + _OVERENGINEERED_PATTERNS
    + _LICENSE_RISK_PATTERNS
)
