"""Ruby rules module."""

from codescope.rules.ruby.security import *

__all__ = [
    "RubySQLInjectionRule",
    "RubyCommandInjectionRule",
    "RubyXSSRule",
    "RubyMassAssignmentRule",
    "RubyHardcodedSecretRule",
]
