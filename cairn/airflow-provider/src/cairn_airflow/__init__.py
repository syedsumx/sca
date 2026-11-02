"""Airflow on the Cairn lakehouse.

* ``cairn`` connections hold a pipeline user's Cairn token: tasks run as that Cairn user, so Cairn's
  policies, masks and row filters apply, and lineage and the audit trail name it.
* :class:`~cairn_airflow.operators.CairnTrinoOperator` runs SQL on Trino with a fresh short-lived Trino
  token per task (never a stored database password).
* :class:`~cairn_airflow.operators.CairnDbtOperator` runs ``cairn dbt`` (dbt on Trino, the run recorded
  in Cairn) from its own virtualenv, so dbt's dependencies never meet Airflow's.
* :mod:`cairn_airflow.auth` signs people in to Airflow's UI through Cairn (OAuth2), with Cairn groups
  mapped to Airflow roles, and (CAIRN_AIRFLOW_TEAM_ROLES) to team roles that see only their DAGs.
* :class:`~cairn_airflow.triggers.CairnDatasetChangedTrigger` runs DAGs when the tables they read change
  (Airflow 3 event-driven scheduling), whoever wrote them.
* :mod:`cairn_airflow.quality`: a gate (fail now) and a sensor (wait) on Cairn's quality status.
* :class:`~cairn_airflow.dbt.CairnDbtTaskGroup`: a dbt project as one task per model.
* :class:`~cairn_airflow.secrets.CairnSecretsBackend`: Airflow's own Trino and SQL operators with a
  fresh token as the pipeline user.
* :class:`~cairn_airflow.lineage.CairnTokenProvider`: OpenLineage to Cairn without a stored key.
"""

__version__ = "0.1.0"


def get_provider_info() -> dict:
    return {
        "package-name": "cairn-airflow-provider",
        "name": "Cairn",
        "description": "Cairn lakehouse: Trino and dbt as a Cairn identity, lineage and sign-in through Cairn.",
        "versions": [__version__],
        "connection-types": [{"connection-type": "cairn", "hook-class-name": "cairn_airflow.hooks.CairnHook"}],
    }
