"""JavaScript/TypeScript rules module."""

from codescope.rules.javascript.security import *
from codescope.rules.javascript.smells import *

__all__ = [
    # Security rules
    "JSXSSRule",
    "JSEvalRule",
    "JSCommandInjectionRule",
    "JSHardcodedSecretRule",
    "JSPrototypePollutionRule",
    # Code smell rules
    "JSLongFunctionRule",
    "JSDeepNestingRule",
    "JSTooManyParametersRule",
]
