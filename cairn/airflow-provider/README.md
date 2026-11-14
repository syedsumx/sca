# cairn-airflow-provider

Airflow 3 on the Cairn lakehouse, with tasks running as Cairn identities:

- a `cairn` connection (a pipeline user's Cairn token), and `CairnTrinoOperator` and `CairnDbtOperator`, which
  sign in to Trino with a fresh short-lived token per task (no stored database password);
- `CairnDbtTaskGroup` (a dbt project as one task per model), `CairnSparkApplicationOperator` (a Spark application),
  and quality gates and sensors on Cairn's quality status (`CairnQualityGateOperator`, `CairnQualitySensor`);
- `CairnDatasetChangedTrigger`: DAGs that run when the tables they read change, whoever wrote them;
- OpenLineage to Cairn without a stored key, and sign-in to Airflow's UI through Cairn (`cairn_airflow.auth`).

Installed in the `cairn/airflow` image (built from `docker/airflow.Dockerfile`). Operators' guide: "Managed
Airflow" and "Airflow, both ways" in `docs/site/administration.md`.

Airflow drives Cairn, too: `cairn_airflow.flow_runs.CairnFlowRunOperator` starts a Cairn flow with parameters as the
connection's Cairn identity and waits for it (deferrable, on the triggerer), failing the task when the run fails and
cancelling the run when the task is killed; `CairnFlowRunSensor` waits for a run (by id, or a flow's latest).
