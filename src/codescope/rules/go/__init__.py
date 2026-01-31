"""Go rules module."""

from codescope.rules.go.security import *

__all__ = [
    "GoSQLInjectionRule",
    "GoCommandInjectionRule",
    "GoHardcodedSecretRule",
    "GoInsecureTLSRule",
]
