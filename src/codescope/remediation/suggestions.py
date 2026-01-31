"""Remediation suggestions for common code issues."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Remediation:
    """A fix suggestion for a detected issue."""

    rule_id: str
    title: str
    description: str
    fix_example: str = ""
    bad_example: str = ""
    references: list[str] = None  # type: ignore[assignment]
    effort_minutes: int = 5
    auto_fixable: bool = False

    def __post_init__(self):
        if self.references is None:
            self.references = []

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "description": self.description,
            "fix_example": self.fix_example,
            "bad_example": self.bad_example,
            "references": self.references,
            "effort_minutes": self.effort_minutes,
            "auto_fixable": self.auto_fixable,
        }


# ── Remediation database ───────────────────────────────────────────
_REMEDIATIONS: dict[str, Remediation] = {}


def _r(rule_id: str, title: str, description: str, bad: str = "", fix: str = "",
       refs: list[str] | None = None, effort: int = 5, auto: bool = False):
    _REMEDIATIONS[rule_id] = Remediation(
        rule_id=rule_id, title=title, description=description,
        bad_example=bad, fix_example=fix, references=refs or [],
        effort_minutes=effort, auto_fixable=auto,
    )


# -- Security --
_r("python:S501", "Disable SSL verification is dangerous",
   "Disabling SSL certificate verification exposes the application to man-in-the-middle attacks. "
   "Always verify SSL certificates in production.",
   bad="requests.get(url, verify=False)",
   fix="requests.get(url, verify=True)  # or use a custom CA bundle\nrequests.get(url, verify='/path/to/ca-bundle.crt')",
   refs=["https://owasp.org/Top10/A02_2021-Cryptographic_Failures/"], effort=5, auto=True)

_r("python:S502", "Weak cryptography detected",
   "MD5 and SHA1 are cryptographically broken. Use SHA-256 or better for hashing.",
   bad="import hashlib\nh = hashlib.md5(data)",
   fix="import hashlib\nh = hashlib.sha256(data)",
   refs=["https://cwe.mitre.org/data/definitions/328.html"], effort=10, auto=True)

_r("python:S506", "Hardcoded password detected",
   "Hardcoded credentials in source code can be extracted by anyone with access. "
   "Use environment variables or a secrets manager.",
   bad='password = "mysecretpass123"',
   fix='import os\npassword = os.environ["DB_PASSWORD"]',
   refs=["https://cwe.mitre.org/data/definitions/798.html"], effort=15)

_r("python:S508", "SQL injection risk",
   "String concatenation or f-string interpolation in SQL queries allows attackers to inject arbitrary SQL. "
   "Always use parameterized queries.",
   bad='cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")',
   fix='cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))',
   refs=["https://owasp.org/Top10/A03_2021-Injection/", "https://cwe.mitre.org/data/definitions/89.html"], effort=10, auto=True)

_r("python:S509", "Command injection risk",
   "Using shell=True with dynamic arguments allows command injection. Use list-form subprocess calls.",
   bad='subprocess.run(f"grep {user_input} file.txt", shell=True)',
   fix='subprocess.run(["grep", user_input, "file.txt"])',
   refs=["https://cwe.mitre.org/data/definitions/78.html"], effort=10, auto=True)

_r("python:S510", "Path traversal risk",
   "User-controlled file paths can be used to read or write arbitrary files. "
   "Validate and sanitize paths, or use a whitelist.",
   bad='with open(user_path) as f:\n    data = f.read()',
   fix='from pathlib import Path\nsafe = Path(base_dir) / Path(user_path).name\nif not safe.resolve().is_relative_to(Path(base_dir).resolve()):\n    raise ValueError("Invalid path")\nwith open(safe) as f:\n    data = f.read()',
   refs=["https://cwe.mitre.org/data/definitions/22.html"], effort=15)

_r("python:S511", "Insecure deserialization",
   "pickle.loads() with untrusted data can execute arbitrary code. Use JSON or a safe serialization format.",
   bad='import pickle\ndata = pickle.loads(untrusted_bytes)',
   fix='import json\ndata = json.loads(untrusted_string)',
   refs=["https://owasp.org/Top10/A08_2021-Software_and_Data_Integrity_Failures/"], effort=20)

# -- Bugs --
_r("python:S301", "Mutable default argument",
   "Mutable default arguments (list, dict, set) are shared between all calls. This causes unexpected behavior.",
   bad="def add_item(item, items=[]):\n    items.append(item)\n    return items",
   fix="def add_item(item, items=None):\n    if items is None:\n        items = []\n    items.append(item)\n    return items",
   effort=5, auto=True)

_r("python:S302", "Comparison using 'is' with literal",
   "Use '==' for value comparison. 'is' checks identity, not equality. It works with small integers by accident.",
   bad="if x is 42:\n    ...",
   fix="if x == 42:\n    ...",
   effort=2, auto=True)

# -- Code Smells --
_r("python:S138", "Function too long",
   "Long functions are hard to understand, test, and maintain. Extract logical sections into helper functions.",
   bad="def process_data(data):\n    # 100+ lines of code\n    ...",
   fix="def process_data(data):\n    validated = _validate(data)\n    transformed = _transform(validated)\n    return _save(transformed)",
   effort=30)

_r("python:S107", "Too many parameters",
   "Functions with many parameters are hard to use and test. Group related parameters into a dataclass or dict.",
   bad="def create_user(name, email, age, address, phone, role, dept, manager):\n    ...",
   fix="@dataclass\nclass UserInfo:\n    name: str\n    email: str\n    age: int\n    address: str = ''\n    phone: str = ''\n\ndef create_user(info: UserInfo, role: str):\n    ...",
   effort=20)

_r("python:S1192", "Duplicated string literal",
   "Repeated string literals make code harder to maintain. Extract into a named constant.",
   bad='if status == "active":\n    ...\nelif status == "active":\n    ...',
   fix='STATUS_ACTIVE = "active"\nif status == STATUS_ACTIVE:\n    ...',
   effort=5, auto=True)

# -- JavaScript --
_r("javascript:S506", "Hardcoded secret in JS",
   "API keys and secrets in frontend JavaScript are visible to all users. Use environment variables and a backend proxy.",
   bad='const API_KEY = "sk_live_abc123...";',
   fix="const API_KEY = process.env.REACT_APP_API_KEY;\n// Or fetch from backend: const key = await fetch('/api/config').then(r => r.json());",
   refs=["https://owasp.org/Top10/A02_2021-Cryptographic_Failures/"], effort=15)

_r("javascript:S508", "DOM-based XSS",
   "Using innerHTML or dangerouslySetInnerHTML with user input enables cross-site scripting. Use textContent or sanitize.",
   bad='element.innerHTML = userInput;',
   fix='element.textContent = userInput;\n// Or use DOMPurify: element.innerHTML = DOMPurify.sanitize(userInput);',
   refs=["https://cwe.mitre.org/data/definitions/79.html"], effort=10, auto=True)

_r("javascript:S509", "eval() usage",
   "eval() executes arbitrary code and is a major security risk. Use safer alternatives.",
   bad='const result = eval(userExpression);',
   fix='// Use JSON.parse for data\nconst data = JSON.parse(jsonString);\n// Use Function constructor if truly needed (still risky)\nconst fn = new Function("x", "return x * 2");',
   refs=["https://cwe.mitre.org/data/definitions/95.html"], effort=15)


class RemediationEngine:
    """Look up remediation suggestions for detected issues."""

    def __init__(self) -> None:
        self._db = dict(_REMEDIATIONS)

    def get(self, rule_id: str) -> Optional[Remediation]:
        """Get remediation for a specific rule."""
        return self._db.get(rule_id)

    def get_for_issue(self, issue: dict) -> Optional[dict]:
        """Get remediation for an issue dict (from scan results)."""
        rule_id = issue.get("rule_id", "")
        rem = self._db.get(rule_id)
        if rem:
            return rem.to_dict()

        # Try partial match (e.g. "python:S5" prefix for security rules)
        for rid, r in self._db.items():
            if rule_id.startswith(rid.rsplit(":", 1)[0] + ":"):
                # Same language prefix — check if category matches
                issue_type = issue.get("type", "")
                if issue_type == "VULNERABILITY" and "security" in str(r.references).lower():
                    return r.to_dict()
        return None

    def get_batch(self, issues: list[dict]) -> list[dict]:
        """Get remediations for a batch of issues."""
        results = []
        for issue in issues:
            rem = self.get_for_issue(issue)
            results.append({
                "issue": issue,
                "remediation": rem,
            })
        return results

    def list_all(self) -> list[dict]:
        """List all available remediations."""
        return [r.to_dict() for r in self._db.values()]

    def add_custom(self, rule_id: str, title: str, description: str, **kwargs) -> None:
        """Add a custom remediation suggestion."""
        self._db[rule_id] = Remediation(
            rule_id=rule_id, title=title, description=description,
            fix_example=kwargs.get("fix_example", ""),
            bad_example=kwargs.get("bad_example", ""),
            references=kwargs.get("references", []),
            effort_minutes=kwargs.get("effort_minutes", 5),
            auto_fixable=kwargs.get("auto_fixable", False),
        )
