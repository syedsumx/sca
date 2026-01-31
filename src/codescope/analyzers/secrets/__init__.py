"""Secret scanning module — detect leaked credentials in code."""

from codescope.analyzers.secrets.scanner import SecretScanner, SecretFinding

__all__ = ["SecretScanner", "SecretFinding"]
