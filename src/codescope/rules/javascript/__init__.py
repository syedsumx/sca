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
    "JSSQLInjectionRule",
    "JSNoSQLInjectionRule",
    "JSPathTraversalRule",
    "JSReDoSRule",
    "JSOpenRedirectRule",
    "JSInsecureCookieRule",
    "JSInsecureRandomnessRule",
    "JSSSRFRule",
    "JSWeakCryptoRule",
    # Code smell rules
    "JSLongFunctionRule",
    "JSDeepNestingRule",
    "JSTooManyParametersRule",
    "JSConsoleLogRule",
    "JSUnusedVariableRule",
    "JSMagicNumberRule",
    "JSEmptyCatchRule",
    "JSCallbackHellRule",
    "JSDuplicatedStringsRule",
    "JSEmptyFunctionRule",
    "JSNoVarRule",
    "JSNoAsyncWithoutAwaitRule",
    "JSTripleEqualsRule",
]
