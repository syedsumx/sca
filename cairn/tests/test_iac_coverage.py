"""The coverage gate of "everything as code": every route that changes something (POST, PUT, PATCH, DELETE) is one of

* **configuration**: a call of a resource type in the registry (cairn.iac.registry: bundles, Terraform, export), or
  on COVERED_BY: another way to change what a resource type manages (the type is the way to keep it in git);
* an **action** (ACTIONS): it does something now (runs, queries, checks, syncs, answers a protocol) and keeps no
  configuration of its own;
* **not configuration** (EXCLUDED), with a reason that starts with its category: ``runtime state`` (made by using
  Cairn: people's own things, records, results), ``data``, ``credential`` (minted by Cairn and shown once),
  ``identity provider`` (pushed by the IdP), ``release`` (a version or deployment of code), ``workflow`` (a request,
  review or approval and its decisions), or ``deployment: <module path>`` (set where Cairn is deployed, by the
  platform's Terraform module).

A new route that changes something and is none of these fails here; so does an entry that no longer matches a route.
Routes are read from the source (every router's decorators, with their prefixes), so features that are switched
off in this test's configuration are checked too. Also checked: the registry's own rules, the Terraform provider's
generated resource table and README being up to date, and the examples being valid against their kinds.

Entries are ``"METHOD /path"`` (paths with ``{}`` for parameters) or ``"/path"`` for every changing method of it."""

import ast
import os
import re

import pytest

from cairn.iac import registry

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "cairn")
METHODS = {"get", "post", "put", "patch", "delete"}
CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
CATEGORIES = ("runtime state", "data", "credential", "identity provider", "release", "workflow", "deployment: ")
LICENCE = ("deployment: deploy/terraform/modules/cairn (variable licence or licence_file: the Kubernetes Secret the "
           "chart installs the licence from, cairn.licence.existingSecret); a hosted tenant's in tenant_licences")

# ---- configuration managed through a resource type, by another call -----------------------------------------------
COVERED_BY: dict[str, str] = {
    "PUT /governance/policies": "policies",                # the whole policy bundle at once; each policy is one
    "PUT /notify/routes": "notification_routes",           # every route at once
    "POST /sql/dashboards/{}/actions": "dashboards",       # a dashboard's actions are its `actions`
    "DELETE /sql/dashboards/{}/actions/{}": "dashboards",
    "POST /sql/dashboards/import/grafana": "dashboards",   # a Grafana dashboard made a dashboard (the kind keeps it)
    "POST /sql/dashboards/{}/copy": "dashboards",          # a copy is a new dashboard of the person's
    "PUT /v1/flows/{}/sharing": "flows",                   # editors and viewers
    "POST /v1/flows/{}/enabled": "flows",
    "POST /v1/flows/{}/publish": "flows",                  # a flow declared as code is published as it is saved
    "POST /v1/nifi/flows/{}/start": "nifi_flows",           # whether a NiFi flow runs is its `running`
    "POST /v1/nifi/flows/{}/stop": "nifi_flows",
    "POST /v1/streams/{}/enabled": "streams",
    "POST /governance/automations/{}/enabled": "automations",
    "POST /v1/people/users/{}/activate": "people",          # a destroyed (deactivated) person, declared again
}

# ---- actions: they do something now, and keep no configuration ---------------------------------------------------
_RUN = "runs or refreshes it now (its schedule and settings are the resource type's)"
_QUERY = "a query, question or computation: an answer, nothing kept as configuration"
_PROTOCOL = "a protocol call from another component (Trino, the catalog, the store, agents, data planes), answered now"
_INBOUND = "an event sent to Cairn by another system (a webhook), handled as it arrives"
_CHECK = "checks, previews, plans or validates: nothing is changed"
_SYNC = "syncs or imports now from what is already configured"
ACTIONS: dict[str, str] = {
    # external MCP servers (L155): calling one of their tools is using them, not configuring Cairn
    "POST /v1/ai/mcp-servers/{}/tools/{}/call": "calls a registered server's tool now, as the caller",
    # the release feed (L159): which feed and channel an install follows is deployment configuration (Helm)
    "POST /v1/system/updates/check": "checks the release feed now; installs nothing",
    # scheduled updates (L160): a plan is an operation on this install, decided now; the policy is update_policy
    "POST /v1/system/updates/plans": "plans an update to a release, as the feed verifies it now",
    "POST /v1/system/updates/plans/{}/approve": "approves a planned update",
    "POST /v1/system/updates/plans/{}/cancel": "cancels a planned update that has not started",
    # managed Postgres: operated now; what it is (size, parameters, schedules) is its resource types'
    "POST /v1/databases/instances/{}/switchover": "promotes a standby now (the instance's shape is postgres_instances)",
    "POST /v1/databases/instances/{}/restart": "restarts the instance now (a rolling restart on Kubernetes)",
    "POST /v1/databases/instances/{}/apply-pending": "applies changes waiting for the maintenance window, now",
    "POST /v1/databases/instances/{}/backups/{}/verify": _CHECK,
    # managed Airflow: operated now; what a deployment is (settings, version, hibernation) is airflow_deployments
    "POST /v1/airflow/deployments/{}/hibernate": "stops the deployment now (its schedule is airflow_deployments)",
    "POST /v1/airflow/deployments/{}/wake": "starts a hibernating deployment now",
    "POST /v1/airflow/promotions/{}/approve": "approves a promotion waiting for approval, which deploys it",
    "POST /v1/airflow/promotions/{}/reject": "rejects a promotion waiting for approval",
    # managed NiFi: what a flow is (definition, parameters, sink) is nifi_flows; these act on it now
    "POST /v1/nifi/flows/{}/deploy": "deploys the flow as it is kept now (a redeploy; its definition is nifi_flows)",
    "POST /v1/nifi/flows/{}/sync": _SYNC,
    # running and refreshing what is declared
    "POST /sources/{}/run": _RUN, "POST /sources/{}/cancel": _RUN, "POST /v1/jobs/{}/run": _RUN,
    "POST /v1/derived/{}/refresh": _RUN, "POST /v1/vectors/{}/refresh": _RUN, "POST /v1/pipes/{}/sweep": _RUN,
    "POST /v1/document-sources/{}/run": _RUN, "POST /v1/reverse-syncs/{}/run": _RUN, "POST /v1/saas-syncs/{}/run": _RUN,
    "POST /v1/sql-workflows/{}/run": _RUN, "POST /sql/reports/{}/run": _RUN, "POST /sql/alerts/{}/check": _RUN,
    "POST /sql/metric-alerts/{}/check": _RUN, "POST /quality/monitors/{}/run": _RUN, "POST /quality/auto/sweep": _RUN,
    "POST /governance/automations/{}/run": _RUN, "POST /governance/playbooks/{}/run": _RUN,
    "POST /v1/ml/models/{}/monitor/run": _RUN, "POST /v1/ml/feature-tables/{}/online/sync": _RUN,
    "POST /v1/serving/{}/sync": _RUN, "POST /v1/lakehouse/parquet-tables/{}/refresh": _RUN, "POST /maintenance/run": _RUN,
    "POST /v1/flows/{}/runs": _RUN, "POST /v1/flows/{}/backfill": _RUN, "POST /v1/flows/{}/airflow-runs": _RUN,
    "POST /v1/flows/runs/{}/cancel": _RUN, "POST /v1/flows/runs/{}/rerun": _RUN,
    # pipeline observability (L145): what is observed is the kinds observability_data_products, _pipeline_slas,
    # _exporters; these act now
    "POST /v1/observability/pipelines/sync": _SYNC,                    # reads the configured Airflows' runs now
    "POST /v1/observability/pipelines/{}/runs/{}/rerun": _RUN,         # clears a failed run's failed tasks
    "POST /v1/observability/exporters/apply": "writes the exporters (observability_exporters) where the Collector "
                                              "reads them, with their header values read from their scopes now",
    "POST /v1/flows/runs/{}/steps/{}/execute": _RUN, "POST /v1/custom-agents/{}/run": _RUN,
    "POST /v1/ontology/runs": _RUN, "POST /v1/ontology/runs/{}/cancel": _RUN, "POST /v1/ontology/runs/{}/resume": _RUN,
    "POST /v1/ontology/runs/{}/import": _RUN, "POST /pipelines/dbt/runs": _RUN, "POST /pipelines/dbt/docs": _RUN,
    "POST /agents/tasks": _RUN, "POST /agents/tasks/{}/cancel": _RUN, "POST /ai/research": _RUN,
    "POST /ai/research/{}/cancel": _RUN, "POST /ai/steward/run": _RUN, "POST /ai/index/rebuild": _RUN,
    "POST /ai/scheduled-tasks/{}/run": _RUN, "POST /v1/sandboxes/runs": _RUN, "POST /v1/agent-abuse/scan": _RUN,
    "POST /governance/posture/scan": _RUN, "POST /governance/privacy/retention/enforce": _RUN,
    "POST /reports/refresh": _RUN, "POST /v1/warehouses/{}/start": _RUN, "POST /v1/procedures/{}/call": _RUN,
    "POST /v1/audit/siem/{}/rewind": "sends the audit trail to a SIEM sink again from a point (its sink is configured)",
    "POST /v1/mirrors/{}/pause": "pauses replication now (an operation; the mirror's declaration is unchanged)",
    "POST /v1/mirrors/{}/resume": "resumes replication now (an operation; the mirror's declaration is unchanged)",
    "POST /v1/mirrors/{}/sync": _RUN,
    "POST /admin/agents/pause": "stops every agent at once (an emergency switch, undone the same way)",
    "POST /admin/extensions/{}/{}": "switches an installed extension off and on at once (incident response; the "
                                    "extension itself is cairn_extension)",
    "POST /storage/v1/cluster/{}": "repairs or rebalances Cairn Store's copies now",
    "POST /storage/v1/cluster/nodes/{}/{}": "drains, activates or removes a Cairn Store node now (an operation on the "
                                            "running cluster; its nodes are the store's deployment)",
    "POST /v1/flows/from-sql-workflow/{}": "converts a SQL workflow into a flow, once (the flow is then cairn_flow)",
    "POST /v1/airflow/dags/{}/convert": "converts an Airflow DAG into a flow definition, returned (not kept)",
    "POST /v1/delta/convert": "converts a table's metadata to Delta now (the table is data)",
    "POST /v1/lakehouse/tables/{}/{}/restore": "rolls a lakehouse table back to an earlier snapshot, now (data)",
    "POST /history/{}/revert": "puts an asset's metadata back as it was at a point in its history, now",
    "POST /sql/versions/{}/{}/{}/restore": "restores an earlier version of a query, workbook or dashboard, now",
    "POST /v1/git-folders/{}/pull": "pulls the folder's branch now (the folder is cairn_git_folder)",
    "POST /v1/git-folders/{}/push": "pushes the folder's changes now (the folder is cairn_git_folder)",
    "POST /collab/enrich": "suggests descriptions and tags now (accepted ones are catalog metadata)",
    "POST /notify/test": "sends a test notification", "POST /notify/inbox/read": "marks a person's notifications read",
    "POST /notify/actions/{}": "answers a notification's button (the action it names runs)",
    "POST /notify/actions/inline/{}/{}/{}": "answers a notification's button from a signed link",
    "POST /v1/metering/report": "reports usage to the hosting control plane now",
    "POST /v1/billing/portal-link": "returns a link to the billing portal",
    "POST /v1/dataplanes/{}/ping": "checks a data plane is reachable",
    # syncing and importing from what is configured
    "POST /sql/catalog/sync": _SYNC, "POST /context/knowledge/sync": _SYNC, "POST /context/semantic/import": _SYNC,
    "POST /governance/tag-sync/{}/sync": _SYNC, "POST /v1/system-tables/sync": _SYNC, "POST /v1/trino/catalog-sync": _SYNC,
    "POST /v1/trino/functions/sync": _SYNC, "POST /v1/ontology/sync": _SYNC, "POST /v1/external-catalogs/{}/sync": _SYNC,
    "POST /v1/external-catalogs/{}/attach": "attaches a declared external catalog to Trino now (the catalog is "
                                            "cairn_external_catalog)",
    # queries, questions and computations
    "POST /ai/ask": _QUERY, "POST /ai/chat": _QUERY, "POST /chat": _QUERY, "POST /ai/docs/generate": _QUERY,
    "POST /ai/docs/markdown": _QUERY, "POST /ai/explain/lineage": _QUERY, "POST /ai/search/semantic": _QUERY,
    "POST /data/ask": _QUERY, "POST /data/query": _QUERY, "POST /data/sheets/ask": _QUERY,
    "POST /data/sheets/metric": _QUERY, "POST /data/sheets/query": _QUERY, "POST /context/metrics/{}/query": _QUERY,
    "POST /sql/run": _QUERY, "POST /sql/saved/{}/run": _QUERY, "POST /sql/builder": _QUERY,
    "POST /sql/metrics/query": _QUERY, "POST /sql/workbooks/compile": _QUERY, "POST /sql/workbooks/run": _QUERY,
    "POST /sql/dashboards/{}/render": _QUERY, "POST /embed/v1/dashboards/{}/render": _QUERY,
    "POST /v1/observability/tiles/preview": _QUERY, "POST /v1/observability/tiles/options": _QUERY,
    "POST /sql/dashboards/{}/actions/{}/run": "a person clicking a dashboard action (the action is the dashboard's)",
    "POST /v1/statements": _QUERY, "POST /v1/statements/next": _QUERY, "POST /v1/statements/cancel": _QUERY,
    "POST /v1/vectors/{}/answer": _QUERY, "POST /v1/serving/{}/lookup": _QUERY,
    "POST /v1/ml/feature-tables/{}/online/lookup": _QUERY, "POST /v1/ml/serve/{}/invocations": _QUERY,
    "POST /v1/ml/deployments/{}/invocations": _QUERY, "POST /v1/ml/finetunes/{}/generate": _QUERY,
    "POST /v1/agent-apps/{}/invocations": _QUERY, "POST /v1/shares/recipient/agents/{}/invocations": _QUERY,
    "POST /v1/apps/{}/chat": _QUERY, "POST /invocations": _QUERY, "POST /v1/ai/gateway/anthropic/v1/messages": _QUERY,
    "POST /v1/ai/gateway/openai/v1/chat/completions": _QUERY, "POST /v1/ai/gateway/openai/v1/embeddings": _QUERY,
    "POST /v1/trino/functions/ai-search": _QUERY, "POST /v1/trino/{}/ai/v1/chat/completions": _QUERY,
    "POST /v1/trino/{}/ai/v1/messages": _QUERY, "POST /v1/delta-sharing/shares/{}/schemas/{}/tables/{}/query": _QUERY,
    "POST /v1/agent-memory/recall": _QUERY, "POST /v1/agent-intents/classify": _QUERY, "POST /v1/synthetic": _QUERY,
    "POST /v1/ontology/agent": _QUERY, "POST /v1/ontology/estimate": _QUERY, "POST /v1/documents/parse": _QUERY,
    "POST /v1/warehouses/hint": _QUERY, "POST /v1/warehouses/usage": _QUERY, "POST /reports/costs": _QUERY,
    "POST /v1/budgets/{}/check": _QUERY, "POST /governance/pii/classify": _QUERY,
    # checks, previews and plans
    "POST /governance/simulate": _CHECK, "POST /governance/policies/validate": _CHECK, "POST /governance/v1/authorize": _CHECK,
    "POST /governance/consent/purposes/{}/check": _CHECK, "POST /governance/pii/propagation/plan": _CHECK,
    "POST /governance/privacy/retention/check": _CHECK, "POST /governance/privacy/proof/verify": _CHECK,
    "POST /governance/privacy/requests/lookup": _CHECK, "POST /governance/personas/{}/preview": _CHECK,
    "POST /governance/automations/preview": _CHECK, "POST /governance/automations/runs/{}/undo": "undoes one run's changes",
    "POST /marketplace/access-requests/preview": _CHECK, "POST /marketplace/products/{}/publish/{}/plan": _CHECK,
    "POST /quality/contracts/check": _CHECK, "POST /sources/test": _CHECK, "POST /v1/network/egress-rules/check": _CHECK,
    "POST /v1/flows/validate": _CHECK, "POST /v1/ontology/validate-sql": _CHECK, "POST /integrations/git/{}/impact": _CHECK,
    "POST /v1/iac/changes/plan": _CHECK, "POST /v1/people/access-check": _CHECK,
    "POST /v1/observability/alert-rules/test": _CHECK,
    "POST /v1/observability/evaluate": _RUN,              # evaluates the declared alert rules and SLOs now
    # the SRE agent (L142): it works now; what it is configured with is sre_agent_settings and sre_runbooks
    "POST /v1/sre/incidents/{}/investigate": "the SRE agent investigates an incident now (its settings and runbooks "
                                             "are sre_agent_settings and sre_runbooks)",
    "POST /v1/sre/incidents/{}/ask": _QUERY, "POST /v1/sre/instances/{}/diagnose": _QUERY,
    "POST /v1/sre/actions/{}/approve": "runs a proposal now as the approving person (an instance operation, a silence "
                                       "or a change request applied through /v1/iac/changes)",
    # protocols between components
    "POST /v1/trino/{}/allow": _PROTOCOL, "POST /v1/trino/{}/batch": _PROTOCOL, "POST /v1/trino/{}/column-mask": _PROTOCOL,
    "POST /v1/trino/{}/row-filters": _PROTOCOL, "POST /v1/trino/{}/lineage": _PROTOCOL, "POST /v1/trino/{}/queries": _PROTOCOL,
    "POST /v1/trino/{}/warehouse/{}/allow": _PROTOCOL, "POST /v1/trino/{}/warehouse/{}/batch": _PROTOCOL,
    "POST /v1/trino/{}/warehouse/{}/column-mask": _PROTOCOL, "POST /v1/trino/{}/warehouse/{}/row-filters": _PROTOCOL,
    "POST /v1/lakehouse/decide": _PROTOCOL, "POST /v1/lakehouse/changes": _PROTOCOL, "POST /v1/storage/decide": _PROTOCOL,
    "POST /agents/{}/heartbeat": _PROTOCOL, "POST /agents/{}/tasks/poll": _PROTOCOL,
    "POST /agents/{}/runs/{}/batches": _PROTOCOL, "POST /agents/{}/runs/{}/complete": _PROTOCOL,
    "POST /agents/{}/runs/{}/logs": _PROTOCOL, "POST /dataplanes/tunnel/{}/poll": _PROTOCOL,
    "POST /dataplanes/tunnel/{}/calls/{}/answer": _PROTOCOL, "POST /dataplanes/tunnel/{}/opa/{}": _PROTOCOL,
    "POST /oauth/authorize": "signing in (OAuth 2.0): a consent answered now",
    "POST /oauth/device": "signing in (OAuth 2.0 device flow): a code confirmed now",
    "POST /v1/sso/saml/acs": "signing in (SAML): an assertion answered now",
    "POST /v1/passkeys/register/options": _PROTOCOL, "POST /v1/passkeys/step-up/options": _PROTOCOL,
    "POST /v1/passkeys/step-up": "a person confirming an action with their passkey, now",
    "POST /v1/flows/runs/{}/steps/{}/{}": "an approval step of a run, answered (the flow is cairn_flow)",
    # events sent to Cairn
    "POST /integrations/slack/commands": _INBOUND, "POST /integrations/slack/events": _INBOUND,
    "POST /integrations/slack/interactions": _INBOUND, "POST /integrations/teams/messages": _INBOUND,
    "POST /integrations/git/{}/webhook": _INBOUND, "POST /integrations/git/writeback": _INBOUND,
    "POST /v1/billing/stripe/webhook": _INBOUND, "POST /openlineage/api/v1/lineage": _INBOUND,
    "POST /notify/inbound/{}": _INBOUND, "POST /quality/external/{}": _INBOUND, "POST /v1/pipes/events": _INBOUND,
}

# ---- not configuration, and why --------------------------------------------------------------------------------
_OBS_INCIDENT = "runtime state: an incident and its timeline, opened by alerts and worked by people and agents"
_MINE = "runtime state: a person's own {}, made by using Cairn (not the workspace's configuration)"
EXCLUDED: dict[str, str] = {
    # the vendor's fleet console (L161): a service of its own (cairn fleet serve), never a customer Cairn's configuration
    "/fleet/v1/installs": "deployment: src/cairn/fleet/server.py (the vendor's own console registers installs)",
    "/fleet/v1/installs/{}": "deployment: src/cairn/fleet/server.py (the vendor's own console registers installs)",
    "POST /fleet/v1/installs/{}/rotate": "credential: an install's check-in key, rotated on the vendor's console",
    "POST /fleet/v1/checkin": "runtime state: an install's check-in to its vendor's fleet console",
    # runtime state: what people make by using Cairn, records and results
    "/ai/artifacts": "runtime state: what assistants produce in a conversation",
    "/ai/artifacts/{}": "runtime state: what assistants produce in a conversation",
    "POST /ai/artifacts/{}/share": "runtime state: what assistants produce in a conversation, shared",
    "/ai/memory": _MINE.format("assistant notes"), "/ai/memory/{}": _MINE.format("assistant notes"),
    "PUT /ai/memory/settings": _MINE.format("assistant memory switch"),
    "/ai/scheduled-tasks": _MINE.format("assistant tasks, made in conversation"),
    "/ai/scheduled-tasks/{}": _MINE.format("assistant tasks, made in conversation"),
    "POST /ai/feedback": "runtime state: a person's rating of an answer",
    "/collab/bookmarks/{}": _MINE.format("bookmarks"), "/collab/follows/{}": _MINE.format("follows"),
    "POST /collab/views": "runtime state: a record that a person looked at an asset",
    "/collab/comments/{}/reactions": "runtime state: people's reactions to comments",
    "POST /collab/threads": "runtime state: people's conversations",
    "POST /collab/threads/{}/comments": "runtime state: people's conversations",
    "POST /collab/threads/{}/{}": "runtime state: people's conversations",
    "/sql/comments/{}": "runtime state: people's comments", "POST /sql/comments/{}/{}": "runtime state: people's comments",
    "/sql/folders/{}/items/{}/{}": "runtime state: where a person files an item in their folders",
    "PUT /notify/settings": _MINE.format("notification preferences"),
    "PUT /v1/me/profile": _MINE.format("profile"),
    "/v1/me/agent-memories": _MINE.format("agent memories"), "/v1/me/agent-memories/{}": _MINE.format("agent memories"),
    "/v1/agent-memory": "runtime state: what agents remember about the people they serve",
    "/v1/agent-memory/{}": "runtime state: what agents remember about the people they serve",
    "/marketplace/products/{}/subscription": _MINE.format("subscriptions to data products"),
    "/v1/databases": "runtime state: a database provisioned on demand, with credentials handed out to its owner; "
                     "declare who may create them with policies",
    "/v1/databases/{}": "runtime state: a database provisioned on demand (declare who may create them with policies)",
    "POST /v1/databases/{}/branches": "runtime state: a branch of a database, made on demand",
    "POST /v1/databases/instances/{}/backups": "runtime state: a base backup taken now (its schedule is the "
                                               "postgres_backup_schedules resource)",
    "DELETE /v1/databases/instances/{}/backups/{}": "runtime state: a base backup deleted before its retention ends",
    "POST /v1/databases/instances/{}/restore": "runtime state: a new instance restored from backups on demand (it is "
                                               "then a postgres_instances resource like any other)",
    "POST /v1/airflow/deployments/{}/deploys": "runtime state: a deploy (a DAG bundle or a team image, versioned and "
                                               "recorded), made from a project or CI, like a release",
    "POST /v1/airflow/deployments/{}/deploys/{}/rollback": "runtime state: an earlier deploy deployed again",
    "POST /v1/airflow/deployments/{}/promote": "runtime state: a deploy promoted to the next environment (a release)",
    "/v1/ml/finetunes": "runtime state: a fine-tuning run", "/v1/ml/finetunes/{}": "runtime state: a fine-tuning run",
    "/v1/tables/{}/refs": "runtime state: branches and tags of a table's data, like git refs",
    "/v1/tables/{}/refs/{}": "runtime state: branches and tags of a table's data, like git refs",
    "PUT /v1/bundles/{}": "runtime state: cairn bundle's own state (what a bundle made), kept for its next plan",
    "/v1/status/incidents": "runtime state: incidents on the status page, posted as they happen",
    "/v1/status/incidents/{}/updates": "runtime state: incidents on the status page, posted as they happen",
    "POST /v1/observability/incidents": _OBS_INCIDENT, "POST /v1/observability/incidents/{}/notes": _OBS_INCIDENT,
    "POST /v1/observability/incidents/{}/acknowledge": _OBS_INCIDENT,
    "POST /v1/observability/incidents/{}/resolve": _OBS_INCIDENT, "POST /v1/observability/incidents/{}/reopen": _OBS_INCIDENT,
    "POST /v1/observability/incidents/{}/assign": _OBS_INCIDENT,
    "POST /v1/observability/alerts/{}/acknowledge": "runtime state: who acknowledged a firing alert",
    "POST /v1/observability/deploys": "release: a deploy, recorded for incident timelines (by CI/CD as it ships)",
    "POST /v1/sre/actions/{}/reject": "workflow: a person's decision on the SRE agent's proposal",
    "POST /v1/sre/incidents/{}/postmortem/draft": "runtime state: an incident's postmortem, drafted by the SRE agent",
    "PUT /v1/sre/incidents/{}/postmortem": "runtime state: an incident's postmortem, edited by a person",
    "POST /v1/sre/incidents/{}/postmortem/approve": "workflow: a person approves an incident's postmortem",
    "POST /v1/chargeback/statements": "runtime state: a chargeback statement, computed for a period",
    "POST /v1/signup": "runtime state: a self-service sign-up (the tenant it makes is the hosting control plane's)",
    "POST /quality/evaluations/{}/feedback": "runtime state: a person's verdict on an evaluation",
    "POST /v1/ml/models/{}/feedback": "data: the truth for a model's answers (labels), recorded as they are known",
    # data
    "/v1/volumes/{}/files/{}": "data: the files in a volume",
    "/sql/input-tables/{}/rows": "data: rows people type into an input table",
    "PATCH /sql/input-tables/{}/rows/{}": "data: rows people type into an input table",
    "POST /v1/ingest/{}/rows": "data: rows written into a table", "POST /v1/serving/{}/rows": "data: rows of a serving table",
    "/v1/samples/{}": "data: a sample dataset loaded to try Cairn, and removed",
    "DELETE /v1/documents/parsed/{}": "data: a parsed document's text",
    "/governance/consent/purposes/{}/records": "data: people's consent, recorded as they give and withdraw it",
    "POST /governance/tokens/domains/{}/tokenize": "data: values tokenised (the domain is cairn_token_domain)",
    "POST /governance/tokens/domains/{}/detokenize": "data: tokens turned back into values, as allowed",
    "POST /governance/tokens/domains/{}/forget": "data: a person's values forgotten (crypto-shredded), on request",
    "POST /v1/tables/{}/refs/{}/publish": "data: a branch's data published to the table's main branch",
    "POST /bulk/import": "data: catalog metadata (descriptions, tags, owners) edited in bulk from a file",
    "POST /bulk/{}/apply": "data: catalog metadata edited in bulk", "POST /bulk/{}/undo": "data: catalog metadata edited in bulk",
    "POST /catalog/assets/terms": "data: a glossary term put on an asset or a column (catalog metadata)",
    "POST /catalog/assets/terms/remove": "data: a glossary term taken off an asset or a column (catalog metadata)",
    "PUT /catalog/assets/domain": "data: an asset put in a domain or taken out (catalog metadata)",
    "POST /catalog/glossary/proposals": "workflow: a glossary term change proposed as a change request (glossary_terms)",
    "POST /v1/clean-rooms/{}/tables": "data: a party contributing a table to a clean room",
    "PUT /v1/clean-rooms/{}/partner/data/{}": "data: an outside party uploading its data to a clean room",
    "POST /v1/clean-rooms/{}/templates/{}/run": "data: a clean room's approved query run on the parties' data",
    "POST /v1/clean-rooms/{}/partner/templates/{}/run": "data: a clean room's approved query run by the outside party",
    # credentials: minted by Cairn and shown once
    "/embed/v1/tokens": "credential: short-lived embedding tokens, minted per use",
    "/embed/v1/tokens/{}": "credential: short-lived embedding tokens, minted per use",
    "/storage/v1/keys": "credential: Cairn Store access keys are minted by the store and their secret shown once; "
                        "service identities are cairn_agent_identity and workload bindings",
    "/storage/v1/keys/{}": "credential: a Cairn Store access key (its secret shown once)",
    "POST /storage/v1/keys/{}/rotate": "credential: a new secret for a Cairn Store access key, shown once",
    "POST /admin/agents/{}/rotate": "credential: a new key for an agent identity, shown once",
    "POST /admin/extensions/{}/rotate": "credential: a new token and signing secret for an extension, shown once",
    "POST /agents/enroll": "credential: an agent enrolling with a one-time token gets its keys",
    "POST /agents/pools/{}/enrollment-tokens": "credential: a one-time enrollment token for a pool, shown once",
    "POST /agents/{}/keys/rotate": "credential: an agent's new keys", "POST /agents/{}/revoke": "credential: an agent's keys revoked",
    "POST /dataplanes/tunnel/enroll": "credential: a data plane enrolling with a one-time token gets its keys",
    "POST /dataplanes/tunnel/{}/keys/rotate": "credential: a data plane's new keys",
    "POST /v1/dataplanes/{}/enrollment-tokens": "credential: a one-time enrollment token for a data plane, shown once",
    "POST /v1/dataplanes/{}/connectors/{}/revoke": "credential: a data plane connector's keys revoked",
    "POST /v1/dataplanes/{}/storage/presign": "credential: a short-lived signed URL",
    "/v1/shares": "credential: a share hands a recipient outside the organisation a credential, shown once (revoked "
                  "with POST /v1/shares/{id}/revoke); declare who may share with policies",
    "POST /v1/shares/{}/revoke": "credential: a share's credential revoked",
    "PUT /v1/shares/{}/package": "credential: part of a share (the agents and metrics its recipient's credential reaches)",
    "/v1/shares/{}/agents": "credential: part of a share, an act between two parties (a credential handed out once)",
    "/v1/shares/{}/agents/{}": "credential: part of a share, an act between two parties (a credential handed out once, "
                               "revoked with POST /v1/shares/{id}/revoke), not configuration kept in git",
    "POST /v1/clean-rooms": "credential: a clean room between parties hands each outside party a secret, shown once",
    "POST /v1/databases/{}/credentials": "credential: a database login, shown once",
    "DELETE /v1/databases/{}/logins/{}": "credential: a database login revoked",
    "POST /v1/volumes/{}/credentials": "credential: short-lived storage credentials for a volume",
    "POST /v1/lakehouse/token": "credential: a short-lived lakehouse token",
    "POST /v1/lakehouse/oauth/tokens": "credential: a short-lived lakehouse token (OAuth 2.0)",
    "POST /v1/lakehouse/sessions/revoke": "credential: lakehouse sessions revoked",
    "POST /v1/shares/oauth/tokens": "credential: a share recipient's short-lived token",
    "POST /v1/external-catalogs/oauth/tokens": "credential: a short-lived token for an attached catalog",
    "POST /v1/trino/token": "credential: a short-lived Trino token", "POST /v1/trino/oauth/token": "credential: a short-lived Trino token",
    "POST /v1/auth/workload-token": "credential: a workload's token exchanged for a short-lived Cairn token "
                                    "(its binding is cairn_workload_binding)",
    "POST /v1/notebooks/token": "credential: a notebook's short-lived token", "POST /v1/sso/token": "credential: a token after SSO",
    "/oauth/token": "credential: OAuth 2.0 tokens", "/oauth/revoke": "credential: OAuth 2.0 tokens revoked",
    "/oauth/register": "credential: an OAuth client registered dynamically, its secret shown once",
    "POST /oauth/device_authorization": "credential: a device code, short-lived",
    "/v1/passkeys/register": "credential: a person's passkey", "/v1/passkeys/{}": "credential: a person's passkey",
    "DELETE /v1/me/devices/{}": "credential: a person's signed-in device", "/admin/devices/{}": "credential: a signed-in device",
    "POST /admin/devices/sign-out": "credential: devices signed out",
    "DELETE /me/agents/{}": "credential: an agent a person connected, disconnected",
    "/v1/me/push/subscriptions": "credential: a browser's push subscription (its keys)",
    "POST /v1/me/push/unsubscribe": "credential: a browser's push subscription removed",
    "POST /governance/tokens/rotate": "credential: tokenisation keys re-wrapped under a new master key",
    "POST /v1/secret-scopes/rotate": "credential: secrets re-encrypted under a new key",
    # identity provider
    "/scim/v2/Groups": "identity provider: SCIM pushes users and groups; declare access to groups with policies, "
                       "personas and grants instead",
    "/scim/v2/Groups/{}": "identity provider: SCIM pushes users and groups; declare access to groups with policies, "
                          "personas and grants instead",
    "/scim/v2/Users": "identity provider (SCIM)", "/scim/v2/Users/{}": "identity provider (SCIM)",
    # releases: versions and deployments of code
    "/v1/agent-apps": "release: each deploy uploads a code bundle and makes a new version (cairn agent deploy, from CI); "
                      "its identity and access are an agent identity and policies",
    "/v1/agent-apps/{}": "release: an agent app's versions (cairn agent deploy, from CI)",
    "/v1/app-packages": "release: an app package published by its publisher",
    "/v1/app-packages/{}/versions": "release: a new version of an app package",
    "/v1/app-installs": "release: installing a published app package (a version) into a workspace, upgraded in place "
                        "by its publisher's releases",
    "/v1/app-installs/{}": "release: an installed app package version",
    "POST /v1/app-installs/{}/upgrade": "release: an app package upgraded",
    "POST /v1/apps/{}/deploy": "release: a data app's code deployed (the app is cairn_data_app)",
    "POST /v1/custom-agents/{}/deploy": "release: a custom agent deployed (the agent is cairn_custom_agent)",
    "POST /v1/custom-agents/{}/undeploy": "release: a custom agent's deployment removed",
    "POST /marketplace/products/{}/publish/{}": "release: a data product published to an outside marketplace",
    "/sql/metrics/{}/publish": "release: a metric published to the catalog (the metric is cairn_metric)",
    "POST /v1/notebooks/publish": "release: a notebook published as a version",
    # workflow: requests, reviews and approvals
    "/v1/iac/changes": "workflow: a change request (proposed, approved, applied) is how code reaches Cairn",
    "/v1/iac/changes/{}": "workflow: a change request; withdrawing one is its DELETE",
    "POST /v1/iac/changes/draft": "workflow: a change request drafted",
    "POST /v1/iac/changes/{}/apply": "workflow: a change request applied",
    "POST /v1/iac/changes/{}/approve": "workflow: a change request approved",
    "POST /v1/iac/changes/{}/reject": "workflow: a change request rejected",
    "POST /v1/iac/changes/{}/commit": "workflow: a change request committed to git",
    "/sql/semantic-models": "workflow: drafts the semantic-model autopilot proposes (accepted ones become metrics, "
                            "cairn_metric)",
    "/sql/semantic-models/{}": "workflow: a semantic-model draft, proposed by the autopilot",
    "POST /sql/semantic-models/{}/accept": "workflow: a semantic-model draft accepted (it becomes metrics)",
    "POST /sql/metrics/{}/certify": "workflow: a steward certifying a metric",
    "POST /sql/input-tables/changes/{}/approve": "workflow: a change to an input table approved by a reviewer",
    "POST /sql/input-tables/changes/{}/reject": "workflow: a change to an input table rejected by a reviewer",
    "/governance/workflows": "workflow: an asset's review (draft, in review, approved)",
    "POST /governance/workflows/{}/{}": "workflow: an asset's review moved on",
    "/governance/access-reviews": "workflow: an access review campaign",
    "POST /governance/access-reviews/{}/close": "workflow: an access review closed",
    "POST /governance/access-reviews/{}/items/{}": "workflow: a reviewer's decision",
    "/governance/ai-systems": "workflow: an AI system registered for review (EU AI Act register: submitted, decided, retired)",
    "/governance/ai-systems/{}": "workflow: an AI system's register entry, changed while in review",
    "POST /governance/ai-systems/{}/submit": "workflow: an AI system submitted for review",
    "POST /governance/ai-systems/{}/decision": "workflow: a reviewer's decision on an AI system",
    "POST /governance/ai-systems/{}/retire": "workflow: an AI system retired from the register",
    "POST /governance/forms/{}/assign": "workflow: a form's tasks assigned (the form is cairn_form)",
    "POST /governance/forms/{}/tasks/{}": "workflow: a form's task answered",
    "/governance/privacy/requests": "workflow: a data subject request",
    "POST /governance/privacy/requests/{}/erase": "workflow: a data subject request carried out",
    "POST /governance/privacy/requests/{}/parts": "workflow: a data subject request",
    "/governance/privacy/holds": "workflow: a legal hold placed on records",
    "POST /governance/privacy/holds/{}/release": "workflow: a legal hold released",
    "/marketplace/access-requests": "workflow: an access request",
    "POST /marketplace/access-requests/{}/{}": "workflow: an access request decided",
    "POST /marketplace/listings/{}/access-requests": "workflow: an access request for a listing",
    "/v1/pipeline-agent/tasks": "workflow: a task the pipeline agent proposes",
    "POST /v1/pipeline-agent/tasks/{}/approve": "workflow: a pipeline agent's task approved",
    "POST /v1/pipeline-agent/tasks/{}/reject": "workflow: a pipeline agent's task rejected",
    "/ai/outbox": "workflow: messages an assistant drafted, sent once a person approves",
    "POST /ai/outbox/{}/approve": "workflow: a drafted message approved",
    "POST /ai/outbox/{}/reject": "workflow: a drafted message rejected",
    "POST /ai/steward/decide": "workflow: a steward's decision on a suggestion",
    "POST /context/findings/{}/{}": "workflow: a context finding accepted or dismissed (accepted ones become definitions)",
    "DELETE /context/definitions/{}": "workflow: a definition approved from the findings, retired",
    "/v1/ontology/proposals/batch": "workflow: ontology proposals decided together",
    "POST /v1/ontology/proposals/{}/{}": "workflow: an ontology proposal accepted or rejected",
    "POST /v1/agent-abuse/findings/{}/dismiss": "workflow: an abuse finding dismissed by a reviewer",
    "POST /reports/costs/propose-deprecation": "workflow: a deprecation proposed for review",
    "PUT /v1/clean-rooms/{}/templates/{}": "workflow: a query proposed to a clean room's parties for approval",
    "POST /v1/clean-rooms/{}/templates/{}/approve": "workflow: a party approving a clean room's query",
    "PUT /v1/clean-rooms/{}/partner/templates/{}": "workflow: an outside party proposing a clean room query",
    "POST /v1/clean-rooms/{}/partner/templates/{}/approve": "workflow: an outside party approving a clean room query",
    # deployment: set where Cairn is deployed
    "/admin/licence": LICENCE,
}


def const_str(node, consts):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = const_str(node.left, consts), const_str(node.right, consts)
        return a + b if a is not None and b is not None else None
    if isinstance(node, ast.JoinedStr):
        out = ""
        for v in node.values:
            inner = v.value if isinstance(v, ast.Constant) else const_str(v.value, consts)
            if inner is None:
                return None
            out += inner
        return out
    return None


def visit(body, scope, consts, path, out, unresolved):
    """Routes in ``body``: decorators of APIRouters (``scope``: router variable -> prefix), nested functions too."""
    for n in body:
        for sub in [n] if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else ast.walk(n):
            if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Call) and \
                    isinstance(sub.value.func, ast.Name) and sub.value.func.id == "APIRouter":
                pre = next((const_str(kw.value, consts) for kw in sub.value.keywords if kw.arg == "prefix"), "")
                for t in sub.targets:
                    if isinstance(t, ast.Name):
                        scope[t.id] = pre
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in METHODS \
                        and isinstance(d.func.value, ast.Name) and d.args:
                    sub = const_str(d.args[0], consts)
                    if scope.get(d.func.value.id) is None or sub is None:
                        unresolved.append(f"{path}:{d.lineno}")
                        continue
                    out.add((d.func.attr.upper(), scope[d.func.value.id] + sub))
            visit(n.body, dict(scope), consts, path, out, unresolved)
        elif isinstance(n, ast.ClassDef):
            visit(n.body, dict(scope), consts, path, out, unresolved)
        elif isinstance(getattr(n, "body", None), list):
            visit(n.body, scope, consts, path, out, unresolved)
            visit(getattr(n, "orelse", []) or [], scope, consts, path, out, unresolved)


def routes() -> set[tuple[str, str]]:
    """Every (METHOD, path) the routers declare, with their prefixes, from the source."""
    out: set[tuple[str, str]] = set()
    unresolved = []
    for dp, _, files in os.walk(SRC):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dp, fn)
            tree = ast.parse(open(path).read())
            consts = {}
            for n in tree.body:
                if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                    v = const_str(n.value, consts)
                    if v is not None:
                        consts[n.targets[0].id] = v

            visit(tree.body, {"app": ""}, consts, path, out, unresolved)
    assert not unresolved, f"routes the gate cannot read (give their paths as constants): {unresolved}"
    return out


def norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path.split("?")[0])


def configurable(rs: set[tuple[str, str]]) -> set[str]:
    """Paths that can be deleted and were created (POST to the collection) or set (PUT/PATCH on themselves)."""
    ops: dict[str, set[str]] = {}
    for m, p in rs:
        ops.setdefault(norm(p), set()).add(m)
    out = set()
    for p, ms in ops.items():
        if "DELETE" not in ms:
            continue
        parent = p.rsplit("/", 1)[0]
        if ms & {"PUT", "PATCH"} or (p.endswith("{}") and "POST" in ops.get(parent, set())):
            out.add(p)
    return out


def kind_calls() -> dict[tuple[str, str], str]:
    """(METHOD, path) of every call a resource type makes -> the type."""
    out = {}
    for k in registry.kinds():
        for o in (k.create, k.update, k.delete):
            if o is not None:
                out[(o.method, norm(o.path))] = k.name
    return out


def covered() -> set[str]:
    return {p for _, p in kind_calls()}


def classified(m: str, p: str) -> list[str]:
    """Where a changing route is classified (more than one place is an error)."""
    out = [name for name, table in (("COVERED_BY", COVERED_BY), ("ACTIONS", ACTIONS), ("EXCLUDED", EXCLUDED))
           if f"{m} {p}" in table or p in table]
    if (m, p) in kind_calls():
        out.append(f"the resource type {kind_calls()[(m, p)]}")
    return out


def test_every_changing_route_is_configuration_an_action_or_excluded_with_a_category():
    rs = routes()
    assert len(rs) > 800, len(rs)                                   # the whole API, switched on or not
    changing = {(m, norm(p)) for m, p in rs if m in CHANGING}
    missing = sorted(f"{m} {p}" for m, p in changing if not classified(m, p))
    assert not missing, ("these routes change something but are neither a call of a resource type in "
                         "cairn.iac.registry (or on COVERED_BY), nor an action on ACTIONS, nor on EXCLUDED with a "
                         "category: " + ", ".join(missing))
    twice = sorted((f"{m} {p}", classified(m, p)) for m, p in changing if len(classified(m, p)) > 1)
    assert not twice, f"classified more than once: {twice}"
    paths = {p for _, p in changing}
    for name, table in (("COVERED_BY", COVERED_BY), ("ACTIONS", ACTIONS), ("EXCLUDED", EXCLUDED)):
        stale = sorted(key for key in table
                       if (tuple(key.split(" ", 1)) not in changing if " " in key else key not in paths))
        assert not stale, f"{name}: no such changing route any more, take them off: {stale}"
    for key, why in EXCLUDED.items():
        assert why.startswith(CATEGORIES) and len(why) > 20, (key, why)
        if why.startswith("deployment: "):
            where = why.split()[1]
            assert os.path.exists(os.path.join(SRC, "..", "..", where)), (key, where)
    for key, why in ACTIONS.items():
        assert len(why) > 10 and not why.startswith(CATEGORIES), (key, why)
    for name in COVERED_BY.values():
        registry.kind(name)


def test_every_configurable_collection_is_code_or_excluded_never_an_action():
    """What can be created (or set) and deleted is configuration unless EXCLUDED says what else it is: an action
    cannot be deleted."""
    rs = routes()
    for p in configurable(rs):
        if p in covered():
            continue
        assert p in EXCLUDED or f"DELETE {p}" in EXCLUDED or any(
            f"{m} {p}" in COVERED_BY or p in COVERED_BY for m in CHANGING), \
            f"{p} can be created and deleted: a resource type, or EXCLUDED with its category"
    assert not set(EXCLUDED) & covered(), "both a resource type and excluded"


def test_the_licence_is_set_by_the_platform_s_terraform_module():
    """The licence is not workspace configuration: the platform module turns it into the Secret the chart reads."""
    root = os.path.join(SRC, "..", "..", "deploy", "terraform", "modules", "cairn")
    text = open(os.path.join(root, "variables.tf")).read() + open(os.path.join(root, "main.tf")).read()
    for needle in ('variable "licence"', 'variable "licence_file"', 'variable "tenant_licences"', "existingSecret"):
        assert needle in text, needle
    assert re.search(r'variable "licence" \{[^}]*sensitive\s*=\s*true', text, re.S)


def test_every_kind_s_paths_are_real_routes():
    known = {(m, norm(p)) for m, p in routes()}
    for k in registry.kinds():
        assert ("GET", norm(k.read)) in known, (k.name, k.read)
        for o in (k.create, k.update, k.delete):
            if o is not None:
                assert (o.method, norm(o.path)) in known, (k.name, o.method, o.path)


def test_the_registry_s_rules_and_examples():
    kinds = registry.kinds()
    registry.check(kinds)
    assert len(kinds) >= 50
    assert set(registry.FAMILIES) == {k.family for k in kinds}
    for k in kinds:
        names = {f.name for f in k.fields}
        for ex in (k.example, k.example_update):
            if ex is None:
                continue
            assert set(ex) <= names, (k.name, set(ex) - names)
        if k.example is not None:
            assert set(k.required) <= set(k.example), (k.name, "the example lacks", set(k.required) - set(k.example))
        for f in k.fields:
            if f.write_only:
                assert f.sensitive, (k.name, f.name, "a write-only value is a secret")
    schema = registry.schema()
    assert [x["name"] for x in schema["kinds"]] == [k.name for k in kinds]


def test_the_terraform_provider_is_generated_from_the_registry():
    """CI fails when zz_generated_kinds.go or the README's resource list was not regenerated."""
    from cairn.iac.terraform import GO_FILE, README, go_source, readme

    assert open(GO_FILE).read() == go_source(), "run: python -m cairn.iac.terraform"
    assert open(README).read() == readme(open(README).read()), "run: python -m cairn.iac.terraform"
    for k in registry.kinds():
        assert f'typeName: "{k.tf}"' in go_source()


def test_the_ui_s_copy_as_code_names_registry_types():
    """Areas of the UI's platform hubs that offer "Copy as Terraform" name a type the registry has, with its id."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ui", "src", "consumer", "resources", "catalog.ts")
    kinds = re.findall(r'"iac": \{"kind": "([a-z_]+)", "id": "([a-z_]+)"\}', open(path).read())
    assert len(kinds) > 20
    for kind, ident in kinds:
        k = registry.kind(kind)
        assert not k.parents and ident == k.id, (kind, ident, k.id)


@pytest.mark.parametrize("name", ["pipes", "secrets", "agent_abuse_settings", "secret_scope_acls"])
def test_ids_split_back_into_parents(name):
    from cairn.iac.engine import split_id, tf_id

    k = registry.kind(name)
    rec = {**{p: "p1" for p in k.parents}, k.id: "x:y/z" if not k.singleton else None}
    parents, ident = split_id(k, tf_id(k, rec))
    assert parents == {p: "p1" for p in k.parents}
    assert ident == ("" if k.singleton else "x:y/z")
