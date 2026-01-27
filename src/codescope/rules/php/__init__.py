"""PHP rules module."""

from codescope.rules.php.security import *

__all__ = [
    "PHPSQLInjectionRule",
    "PHPCommandInjectionRule",
    "PHPXSSRule",
    "PHPFileInclusionRule",
    "PHPHardcodedSecretRule",
]
