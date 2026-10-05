"""Artifact stores used by the demo pipelines.

Each store is a small, swappable adapter. Other backends (Qdrant, Snowflake, ...) can be
added by implementing the same interface and registering a purger in
:mod:`tether.deletion.purgers`.
"""
