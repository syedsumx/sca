"""Rule engine module for CodeScope."""

from codescope.rules.base import Rule, RuleResult
from codescope.rules.registry import RuleRegistry, get_rules, get_rule

# Import all language rules to register them
from codescope.rules import python
from codescope.rules import javascript
from codescope.rules import java
from codescope.rules import go
from codescope.rules import csharp
from codescope.rules import php
from codescope.rules import ruby

# Import API security rules (GraphQL, gRPC)
from codescope.rules import api

__all__ = [
    "Rule",
    "RuleResult",
    "RuleRegistry",
    "get_rules",
    "get_rule",
]
