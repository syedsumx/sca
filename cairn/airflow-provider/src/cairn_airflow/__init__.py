"""Airflow on the Cairn lakehouse.

* ``cairn`` connections hold a pipeline user's Cairn token: tasks run as that Cairn user, so Cairn's
  policies, masks and row filters apply, and lineage and the audit trail name it.
* :class:`~cairn_airflow.operators.CairnTrinoOperator` runs SQL on Trino with a fresh short-lived Trino
  token per task (never a stored database password).
* :class:`~cairn_airflow.operators.CairnDbtOperator` runs ``cairn dbt`` (dbt on Trino, the run recorded
  in Cairn) from its own virtualenv, so dbt's dependencies never meet Airflow's.
* :mod:`cairn_airflow.auth` signs people in to Airflow's UI through Cairn (OAuth2), with Cairn groups
  mapped to Airflow roles.
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
