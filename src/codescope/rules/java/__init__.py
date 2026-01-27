"""Java rules module."""

from codescope.rules.java.security import *

__all__ = [
    "JavaSQLInjectionRule",
    "JavaCommandInjectionRule",
    "JavaXXERule",
    "JavaDeserializationRule",
    "JavaHardcodedSecretRule",
    "JavaPathTraversalRule",
]
