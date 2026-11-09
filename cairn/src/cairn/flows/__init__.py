"""Cairn Flows: orchestration of any steps (SQL, derived tables, checks, notebooks, Python, Spark, dbt, pipes,
streams, agents, Airflow DAGs, approvals, waits, branches, for-each, notifications) as one DAG, run as its owner.

* :mod:`cairn.flows.model`: the definition (``kind: flow``), its validation and its YAML;
* :mod:`cairn.flows.expr`: the expressions and ``{{ }}`` templates in it;
* :mod:`cairn.flows.steps`: what each step does;
* :mod:`cairn.flows.engine`: versions, runs, the scheduler (safe with several replicas);
* :mod:`cairn.flows.api`: ``/v1/flows``; :mod:`cairn.flows.cli`: ``cairn flows``.
"""
