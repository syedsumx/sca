"""C# rules module."""

from codescope.rules.csharp.security import *

__all__ = [
    "CSharpSQLInjectionRule",
    "CSharpCommandInjectionRule",
    "CSharpXXERule",
    "CSharpHardcodedSecretRule",
    "CSharpDeserializationRule",
]
