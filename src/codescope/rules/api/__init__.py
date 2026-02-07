"""API security rules for GraphQL and gRPC."""

from codescope.rules.api import graphql  # noqa: F401
from codescope.rules.api import grpc  # noqa: F401

__all__ = ["graphql", "grpc"]
