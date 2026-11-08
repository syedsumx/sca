import type { ResourceSpec } from './types';

/**
 * The workspace's platform areas: each hub page is a set of tabs, each tab one backend area shown by
 * ResourceView against the real API (its list endpoint, row details and actions). The paths, list keys and
 * fields were read from the API's routers and services; keep them in step when an endpoint changes.
 */
export const RESOURCE_SPECS: Record<string, ResourceSpec> = {
    "agent-listings": {
        "key": "agent-listings",
        "title": "Marketplace listings",
        "path": "/marketplace/listings",
        "listKey": "listings",
        "description": "Agents (agent apps and custom agents) and skills (installed extensions) offered in the marketplace beside data products. Approval of a request grants USE_AGENT (or USE_SKILL) on that one agent or skill, until it expires.",
        "columns": [
            "name",
            "kind",
            "target",
            "source",
            "owners",
            "consumers",
            "access"
        ],
        "actions": [
            {
                "label": "Request access",
                "method": "POST",
                "path": "/marketplace/listings/{id}/access-requests",
                "body": {
                    "purpose": "Operations",
                    "justification": "Triage the support queue each morning",
                    "duration_days": 30
                }
            },
            {
                "label": "Hide",
                "method": "PATCH",
                "path": "/marketplace/listings/{id}",
                "body": {
                    "published": false
                }
            },
            {
                "label": "List an agent or skill",
                "method": "POST",
                "path": "/marketplace/listings",
                "body": {
                    "kind": "agent",
                    "target": "triage",
                    "title": "Ticket triage",
                    "description": "Sorts the support queue by urgency.",
                    "tags": [
                        "support"
                    ],
                    "max_duration_days": 90
                }
            },
            {
                "label": "Remove",
                "method": "DELETE",
                "path": "/marketplace/listings/{id}",
                "body": null
            }
        ]
    },
    "share-usage": {
        "key": "share-usage",
        "title": "Share usage",
        "path": "/v1/shares/usage/overview",
        "listKey": "shares",
        "description": "What each share's recipient reads: requests, table loads, refusals and failures, the last access, and what needs attention: shares nobody uses, shares about to expire and recipients reading tables whose quality is failing.",
        "columns": [
            "name",
            "recipient",
            "state",
            "last_access_ms",
            "requests",
            "table_loads",
            "refusals",
            "unused",
            "expiring_soon"
        ],
        "seconds": [
            "expires_at"
        ],
        "actions": [
            {
                "label": "Revoke",
                "method": "POST",
                "path": "/v1/shares/{id}/revoke",
                "body": {}
            }
        ]
    },
    "external-catalogs": {
        "key": "external-catalogs",
        "title": "External catalogs",
        "path": "/v1/external-catalogs",
        "listKey": "catalogs",
        "rowKey": "name",
        "description": "Other organisations' and cloud Iceberg REST catalogs, queried in Trino as catalogs of their own under the lakehouse's grants, masks and row filters; read-only unless registered writable. Their credentials stay in Cairn.",
        "columns": [
            "name",
            "uri",
            "auth",
            "writable",
            "trino_state",
            "last_sync_ms"
        ],
        "actions": [
            {
                "label": "Sync now",
                "method": "POST",
                "path": "/v1/external-catalogs/{name}/sync",
                "body": {}
            },
            {
                "label": "Add to Trino again",
                "method": "POST",
                "path": "/v1/external-catalogs/{name}/attach",
                "body": {}
            },
            {
                "label": "Make writable",
                "method": "PATCH",
                "path": "/v1/external-catalogs/{name}",
                "body": {
                    "writable": true
                }
            },
            {
                "label": "Register",
                "method": "POST",
                "path": "/v1/external-catalogs",
                "body": {
                    "name": "partner",
                    "uri": "https://catalog.partner.example/iceberg",
                    "warehouse": "shared",
                    "auth": "oauth2",
                    "client_id": "cairn-at-partner",
                    "credential_ref": "scope:partner-catalog/client-secret",
                    "token_url": "https://login.partner.example/oauth/token"
                }
            },
            {
                "label": "Remove",
                "method": "DELETE",
                "path": "/v1/external-catalogs/{name}",
                "body": null
            }
        ]
    },
    "semantic-models": {
        "key": "semantic-models",
        "title": "Semantic model drafts",
        "path": "/sql/semantic-models",
        "listKey": null,
        "description": "Draft semantic models built from tables you can see and how they are queried, which you review and publish as governed metrics.",
        "columns": [
            "connection",
            "tables",
            "status",
            "created_ms"
        ],
        "actions": [
            {
                "label": "Accept metrics",
                "method": "POST",
                "path": "/sql/semantic-models/{id}/accept",
                "body": {
                    "metrics": [
                        "total_revenue",
                        "order_count"
                    ],
                    "edits": {}
                }
            },
            {
                "label": "New draft",
                "method": "POST",
                "path": "/sql/semantic-models",
                "body": {
                    "connection": "lakehouse",
                    "tables": [
                        "sales.orders",
                        "sales.customers"
                    ]
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/sql/semantic-models/{id}",
                "body": null
            }
        ]
    },
    "mirrors": {
        "key": "mirrors",
        "title": "Mirrors",
        "path": "/v1/mirrors",
        "listKey": "mirrors",
        "description": "Keeps a lakehouse table in step with a table in an operational database (PostgreSQL, MySQL, MongoDB) by copying its changes as they happen.",
        "columns": [
            "name",
            "table",
            "target",
            "status",
            "rows",
            "last_commit_ms",
            "enabled"
        ],
        "actions": [
            {
                "label": "Sync now",
                "method": "POST",
                "path": "/v1/mirrors/{name}/sync",
                "body": {}
            },
            {
                "label": "Pause",
                "method": "POST",
                "path": "/v1/mirrors/{name}/pause",
                "body": {}
            },
            {
                "label": "Resume",
                "method": "POST",
                "path": "/v1/mirrors/{name}/resume",
                "body": {}
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/mirrors/{name}",
                "body": null
            }
        ]
    },
    "document-sources": {
        "key": "document-sources",
        "title": "Document sources",
        "path": "/v1/document-sources",
        "listKey": null,
        "description": "Copies files from a SharePoint/OneDrive or Google Drive folder into a store prefix on a schedule, so a files pipe can index them.",
        "columns": [
            "name",
            "kind",
            "bucket",
            "prefix",
            "status",
            "files",
            "last_run_ms"
        ],
        "actions": [
            {
                "label": "Run now",
                "method": "POST",
                "path": "/v1/document-sources/{name}/run",
                "body": {}
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/document-sources/{name}",
                "body": null
            }
        ]
    },
    "saas-syncs": {
        "key": "saas-syncs",
        "title": "SaaS syncs",
        "path": "/v1/saas-syncs",
        "listKey": null,
        "description": "Loads Salesforce, HubSpot or Google Sheets data into a lakehouse table on a schedule, only the records changed since the last run. Lists only your own syncs.",
        "columns": [
            "name",
            "kind",
            "object",
            "target",
            "cron",
            "last_status",
            "last_run_ms"
        ],
        "actions": [
            {
                "label": "Run now",
                "method": "POST",
                "path": "/v1/saas-syncs/{name}/run",
                "body": {}
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/saas-syncs/{name}",
                "body": null
            }
        ]
    },
    "reverse-syncs": {
        "key": "reverse-syncs",
        "title": "Reverse syncs",
        "path": "/v1/reverse-syncs",
        "listKey": null,
        "description": "Sends rows from a lakehouse table out to HubSpot or Salesforce on a schedule, as their owner, sending only new and changed rows. Lists only your own syncs.",
        "columns": [
            "name",
            "source",
            "kind",
            "object",
            "last_status",
            "last_sent",
            "last_run_ms"
        ],
        "actions": [
            {
                "label": "Run now",
                "method": "POST",
                "path": "/v1/reverse-syncs/{name}/run",
                "body": {}
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/reverse-syncs/{name}",
                "body": null
            }
        ]
    },
    "pipes": {
        "key": "pipes",
        "title": "Pipes",
        "path": "/v1/pipes",
        "listKey": null,
        "description": "Loads files that land under a store bucket and prefix (CSV, JSON lines, Parquet, Excel, or any documents) into a lakehouse table, each file once.",
        "columns": [
            "name",
            "bucket",
            "prefix",
            "format",
            "target",
            "enabled",
            "created_ms"
        ],
        "actions": [
            {
                "label": "Sweep now",
                "method": "POST",
                "path": "/v1/pipes/{id}/sweep",
                "body": {}
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/pipes/{id}",
                "body": null
            }
        ]
    },
    "streams": {
        "key": "streams",
        "title": "Streams",
        "path": "/v1/streams",
        "listKey": null,
        "description": "Writes messages from a Kafka topic (or rows posted over HTTP) into a lakehouse table continuously, including database change events.",
        "columns": [
            "name",
            "topic",
            "format",
            "mode",
            "target",
            "enabled",
            "status"
        ],
        "actions": [
            {
                "label": "Enable / disable",
                "method": "POST",
                "path": "/v1/streams/{id}/enabled",
                "body": {
                    "enabled": false
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/streams/{id}",
                "body": null
            }
        ]
    },
    "samples": {
        "key": "samples",
        "title": "Sample datasets",
        "path": "/v1/samples",
        "listKey": null,
        "description": "Built-in sample datasets (a retail shop's customers, products and orders) to try Cairn on before your own data arrives. Operators only.",
        "columns": [
            "name",
            "state",
            "namespace",
            "tables",
            "by",
            "updated_ms"
        ],
        "actions": [
            {
                "label": "Load",
                "method": "POST",
                "path": "/v1/samples/{name}",
                "body": {}
            },
            {
                "label": "Remove",
                "method": "DELETE",
                "path": "/v1/samples/{name}",
                "body": null
            }
        ]
    },
    "derived": {
        "key": "derived",
        "title": "Derived tables",
        "path": "/v1/derived",
        "listKey": null,
        "description": "A SELECT kept as a lakehouse table and refreshed on a schedule or when the tables it reads change, run as its owner.",
        "columns": [
            "name",
            "target",
            "mode",
            "kind",
            "every_minutes",
            "fresh",
            "last_run_ms"
        ],
        "actions": [
            {
                "label": "Refresh",
                "method": "POST",
                "path": "/v1/derived/{id}/refresh",
                "body": {
                    "full": false
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/derived/{id}",
                "body": null
            }
        ]
    },
    "vectors": {
        "key": "vectors",
        "title": "Vector indexes",
        "path": "/v1/vectors",
        "listKey": null,
        "description": "Indexes a lakehouse table's text column so it can be searched by meaning, kept up to date as rows change. Ask with sources answers a question only from the index's passages, as you, citing the passages it used (or says it cannot answer).",
        "columns": [
            "name",
            "source",
            "key",
            "text",
            "chunks",
            "refreshed_ms",
            "enabled"
        ],
        "actions": [
            {
                "label": "Ask with sources",
                "method": "POST",
                "path": "/v1/vectors/{name}/answer",
                "body": {
                    "question": "How long do refunds take?",
                    "k": 8
                }
            },
            {
                "label": "Refresh",
                "method": "POST",
                "path": "/v1/vectors/{name}/refresh",
                "body": {}
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/vectors/{name}",
                "body": null
            }
        ]
    },
    "volumes": {
        "key": "volumes",
        "title": "Volumes",
        "path": "/v1/volumes",
        "listKey": null,
        "description": "Governed folders of files in the store, readable and writable by the people and groups they are granted to.",
        "columns": [
            "name",
            "owner",
            "description",
            "access",
            "path",
            "created_ms"
        ],
        "actions": [
            {
                "label": "Grant access",
                "method": "PUT",
                "path": "/v1/volumes/{name}/grants",
                "body": {
                    "principal": "urn:li:corpuser:raj",
                    "access": "read"
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/volumes/{name}",
                "body": null
            }
        ]
    },
    "storage": {
        "key": "storage",
        "title": "Storage buckets",
        "path": "/storage/v1/buckets",
        "listKey": null,
        "description": "Buckets in Cairn Store with their size, object count, quota and settings.",
        "columns": [
            "name",
            "usage",
            "quota",
            "versioning",
            "disk_type",
            "created"
        ],
        "actions": [
            {
                "label": "Set quota",
                "method": "PUT",
                "path": "/storage/v1/buckets/{bucket}/quota",
                "body": {
                    "max_bytes": 5497558138880,
                    "max_objects": 10000000
                }
            },
            {
                "label": "Remove quota",
                "method": "DELETE",
                "path": "/storage/v1/buckets/{bucket}/quota",
                "body": null
            },
            {
                "label": "Create access key",
                "method": "POST",
                "path": "/storage/v1/keys",
                "body": {
                    "buckets": [
                        "northwind-landing"
                    ],
                    "read_only": false,
                    "description": "POS uploader"
                }
            }
        ]
    },
    "maintenance": {
        "key": "maintenance",
        "title": "Maintenance policies",
        "path": "/maintenance/policies",
        "listKey": null,
        "description": "Schedules that compact small files, expire old snapshots, remove orphan files and keep lakehouse tables clustered, per table, schema or everything (*).",
        "columns": [
            "scope",
            "every_hours",
            "optimize",
            "expire_days",
            "orphan_days",
            "cluster",
            "enabled"
        ],
        "actions": [
            {
                "label": "Save policy",
                "method": "PUT",
                "path": "/maintenance/policies/{scope}",
                "body": {
                    "optimize": true,
                    "optimize_threshold": "128MB",
                    "expire_days": 7,
                    "orphan_days": 7,
                    "every_hours": 24,
                    "cluster": "auto",
                    "enabled": true
                }
            },
            {
                "label": "Delete policy",
                "method": "DELETE",
                "path": "/maintenance/policies/{scope}",
                "body": null
            },
            {
                "label": "Run now",
                "method": "POST",
                "path": "/maintenance/run",
                "body": {
                    "schema": "sales",
                    "table": "orders"
                }
            }
        ]
    },
    "maintenance-runs": {
        "key": "maintenance-runs",
        "title": "Maintenance runs",
        "path": "/maintenance/runs",
        "listKey": null,
        "description": "The latest 200 maintenance operations run on lakehouse tables, with their outcome.",
        "columns": [
            "table_name",
            "operation",
            "status",
            "started_ms",
            "finished_ms",
            "detail"
        ],
        "actions": [
            {
                "label": "Run now",
                "method": "POST",
                "path": "/maintenance/run",
                "body": {
                    "schema": "sales",
                    "table": "orders"
                }
            }
        ]
    },
    "procedures": {
        "key": "procedures",
        "title": "Procedures",
        "path": "/v1/procedures",
        "listKey": null,
        "description": "Named, parameterized SQL scripts run on Trino as whoever calls them. Lists those you own or may call.",
        "columns": [
            "name",
            "owner",
            "connection",
            "description",
            "updated_ms"
        ],
        "actions": [
            {
                "label": "Call",
                "method": "POST",
                "path": "/v1/procedures/{name}/call",
                "body": {
                    "args": {}
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/procedures/{name}",
                "body": null
            }
        ]
    },
    "warehouses": {
        "key": "warehouses",
        "title": "Warehouses",
        "path": "/v1/warehouses",
        "listKey": null,
        "description": "The team Trino warehouses you may use, whether each is running, starting or stopped, and how busy it is.",
        "columns": [
            "name",
            "state",
            "queries_running",
            "queries_queued",
            "idle_minutes",
            "starts"
        ],
        "actions": [
            {
                "label": "Start",
                "method": "POST",
                "path": "/v1/warehouses/{name}/start",
                "body": {}
            }
        ]
    },
    "databases": {
        "key": "databases",
        "title": "Databases",
        "path": "/v1/databases",
        "listKey": "databases",
        "description": "PostgreSQL databases for applications, made through Cairn, with short-lived personal logins.",
        "columns": [
            "name",
            "branch_of",
            "created_by",
            "host",
            "port",
            "created_ms"
        ],
        "actions": [
            {
                "label": "Get credentials",
                "method": "POST",
                "path": "/v1/databases/{name}/credentials",
                "body": {
                    "access": "read",
                    "ttl_minutes": 60
                }
            },
            {
                "label": "Branch",
                "method": "POST",
                "path": "/v1/databases/{name}/branches",
                "body": {
                    "name": "loyalty_app_test"
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/databases/{name}",
                "body": null
            }
        ]
    },
    "jobs": {
        "key": "jobs",
        "title": "Jobs",
        "path": "/v1/jobs",
        "listKey": null,
        "description": "Notebooks and Python scripts that run on a schedule as their owner, each in its own container, with their output kept.",
        "columns": [
            "name",
            "kind",
            "owner",
            "cron",
            "enabled",
            "next_run_ms"
        ],
        "actions": [
            {
                "label": "Run now",
                "method": "POST",
                "path": "/v1/jobs/{name}/run",
                "body": {}
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/jobs/{name}",
                "body": null
            }
        ]
    },
    "apps": {
        "key": "apps",
        "title": "Data apps",
        "path": "/v1/apps",
        "listKey": null,
        "description": "Streamlit or other web apps served by Cairn at /apps/<name>/, each viewer signed in and governed as themselves. Lists the apps you may open.",
        "columns": [
            "name",
            "title",
            "runtime",
            "commit",
            "deployed_ms",
            "running"
        ],
        "actions": [
            {
                "label": "Deploy",
                "method": "POST",
                "path": "/v1/apps/{name}/deploy",
                "body": {}
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/apps/{name}",
                "body": null
            }
        ]
    },
    "spark-history": {
        "key": "spark-history",
        "title": "Spark applications",
        "path": "/v1/spark/history/api/v1/applications",
        "listKey": null,
        "description": "Spark applications you ran (all of them for operators), from the Spark History Server.",
        "columns": [
            "name",
            "attempts"
        ],
        "actions": []
    },
    "agent-apps": {
        "key": "agent-apps",
        "title": "Agent apps",
        "path": "/v1/agent-apps",
        "listKey": null,
        "description": "Code-first agents deployed to Cairn from a project bundle; each runs as the person asking it, within the scopes it was deployed with.",
        "columns": [
            "name",
            "owner",
            "version",
            "status",
            "model",
            "scopes",
            "updated_ms"
        ],
        "actions": [
            {
                "label": "Ask",
                "method": "POST",
                "path": "/v1/agent-apps/{name}/invocations",
                "body": {
                    "input": "Which stores missed their weekly sales target?",
                    "session_id": "s-2026-11-08-1"
                }
            },
            {
                "label": "Deploy / redeploy",
                "method": "POST",
                "path": "/v1/agent-apps",
                "body": {
                    "manifest": {
                        "name": "store-ops-helper",
                        "description": "...",
                        "model": "claude-sonnet",
                        "scopes": [
                            "model",
                            "query_data"
                        ]
                    },
                    "bundle": "<base64 tar.gz>"
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/agent-apps/{name}",
                "body": null
            }
        ]
    },
    "custom-agents": {
        "key": "custom-agents",
        "title": "Custom agents",
        "path": "/v1/custom-agents",
        "listKey": null,
        "description": "Agents a team builds inside Cairn from instructions and a chosen set of assistant tools; they run as the person who starts them, narrowed to the agent's scopes and tools.",
        "columns": [
            "name",
            "owner",
            "team",
            "deployed",
            "version",
            "scopes",
            "updated_ms"
        ],
        "actions": [
            {
                "label": "Run",
                "method": "POST",
                "path": "/v1/custom-agents/{name}/run",
                "body": {
                    "input": "Which product tables have no owner?"
                }
            },
            {
                "label": "Deploy",
                "method": "POST",
                "path": "/v1/custom-agents/{name}/deploy",
                "body": null
            },
            {
                "label": "Undeploy",
                "method": "POST",
                "path": "/v1/custom-agents/{name}/undeploy",
                "body": null
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/custom-agents/{name}",
                "body": null
            }
        ]
    },
    "scheduled-tasks": {
        "key": "scheduled-tasks",
        "title": "Scheduled tasks",
        "path": "/ai/scheduled-tasks",
        "listKey": null,
        "description": "Recurring research the assistant runs for you on a schedule, saving each report as your artifact and optionally queuing it for Slack or email once you approve.",
        "columns": [
            "name",
            "cron",
            "enabled",
            "last_status",
            "last_run_ms",
            "next_run_ms",
            "send"
        ],
        "actions": [
            {
                "label": "Run now",
                "method": "POST",
                "path": "/ai/scheduled-tasks/{id}/run",
                "body": null
            },
            {
                "label": "Pause",
                "method": "PUT",
                "path": "/ai/scheduled-tasks/{id}",
                "body": {
                    "enabled": false
                }
            },
            {
                "label": "Resume",
                "method": "PUT",
                "path": "/ai/scheduled-tasks/{id}",
                "body": {
                    "enabled": true
                }
            },
            {
                "label": "Create",
                "method": "POST",
                "path": "/ai/scheduled-tasks",
                "body": {
                    "name": "Weekly revenue",
                    "prompt": "Summarise last week's revenue by region and anything unusual",
                    "cron": "0 7 * * 1",
                    "max_steps": 4,
                    "send": {
                        "slack": "#sales-leads"
                    }
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/ai/scheduled-tasks/{id}",
                "body": null
            }
        ]
    },
    "outbox": {
        "key": "outbox",
        "title": "Outbox",
        "path": "/ai/outbox",
        "listKey": null,
        "description": "Reports waiting for your approval before Cairn sends them to Slack or email, and the ones already sent, rejected or expired.",
        "columns": [
            "subject",
            "kind",
            "target",
            "status",
            "created_ms",
            "decided_ms"
        ],
        "actions": [
            {
                "label": "Approve and send",
                "method": "POST",
                "path": "/ai/outbox/{id}/approve",
                "body": null
            },
            {
                "label": "Reject",
                "method": "POST",
                "path": "/ai/outbox/{id}/reject",
                "body": null
            }
        ]
    },
    "agent-insights": {
        "key": "agent-insights",
        "title": "Agent insights",
        "path": "/v1/agent-traces/insights",
        "listKey": "agents",
        "description": "Per agent over the last week: runs, tool calls, error rate, loops, slow calls and model cost, compared with the agent's own baseline. Administrators and operators see every agent; owners see their own.",
        "columns": [
            "name",
            "kind",
            "runs",
            "tool_calls",
            "error_rate",
            "loops",
            "slow_calls",
            "p95_ms",
            "last_ms"
        ],
        "rowKey": "agent",
        "actions": [
            {
                "label": "Change thresholds",
                "method": "PUT",
                "path": "/v1/agent-traces/settings",
                "body": {
                    "loop_threshold": 4,
                    "slow_call_ms": 10000,
                    "outlier_factor": 2.0
                }
            }
        ]
    },
    "agent-findings": {
        "key": "agent-findings",
        "title": "Agent findings",
        "path": "/v1/agent-traces/insights",
        "listKey": "findings",
        "description": "What the traces say needs a look, most severe first: an agent stuck repeating a call, failing or refused calls, slow calls, and spend or volume well above its own baseline.",
        "columns": [
            "severity",
            "agent_name",
            "kind",
            "title",
            "value",
            "baseline"
        ]
    },
    "agent-intent-denials": {
        "key": "agent-intent-denials",
        "title": "Intent denials",
        "path": "/v1/agent-intents/denials",
        "listKey": null,
        "description": "Agent requests refused because they fell outside the intent their run declared: a write during a read-only task, an export, a table the task does not concern. Administrators and operators see all; people see those made for them or by agents they own.",
        "columns": [
            "ts",
            "agent",
            "actor",
            "category",
            "reason",
            "purpose"
        ],
        "actions": [
            {
                "label": "Require intents",
                "method": "PUT",
                "path": "/v1/agent-intents/settings",
                "body": {
                    "required": true
                }
            },
            {
                "label": "Make intents optional",
                "method": "PUT",
                "path": "/v1/agent-intents/settings",
                "body": {
                    "required": false
                }
            }
        ]
    },
    "pipeline-agent": {
        "key": "pipeline-agent",
        "title": "Pipeline agent tasks",
        "path": "/v1/pipeline-agent/tasks",
        "listKey": null,
        "description": "Pipelines the agent drafted from a goal in plain words (or fixes for a broken derived table), tried as you without writing anything, and shipped only when you approve.",
        "columns": [
            "target",
            "kind",
            "status",
            "created_ms"
        ],
        "actions": [
            {
                "label": "Approve and ship",
                "method": "POST",
                "path": "/v1/pipeline-agent/tasks/{id}/approve",
                "body": null
            },
            {
                "label": "Reject",
                "method": "POST",
                "path": "/v1/pipeline-agent/tasks/{id}/reject",
                "body": null
            },
            {
                "label": "New task",
                "method": "POST",
                "path": "/v1/pipeline-agent/tasks",
                "body": {
                    "goal": "Daily revenue per region from paid orders",
                    "target": "analytics.daily_revenue",
                    "datasets": [],
                    "checks": [
                        {
                            "name": "no_negative",
                            "sql": "SELECT count(*) FROM candidate WHERE revenue < 0"
                        }
                    ]
                }
            }
        ]
    },
    "ai-steward": {
        "key": "ai-steward",
        "title": "AI steward proposals",
        "path": "/ai/steward/proposals",
        "listKey": null,
        "description": "Owners, glossary terms, tags and descriptions the AI steward proposes for datasets, for you to accept or reject in batches.",
        "columns": [
            "name",
            "kind",
            "field",
            "value",
            "confidence",
            "state"
        ],
        "actions": [
            {
                "label": "Accept",
                "method": "POST",
                "path": "/ai/steward/decide",
                "body": {
                    "ids": [
                        "sp_..."
                    ],
                    "action": "accept",
                    "edits": {}
                }
            },
            {
                "label": "Reject",
                "method": "POST",
                "path": "/ai/steward/decide",
                "body": {
                    "ids": [
                        "sp_..."
                    ],
                    "action": "reject"
                }
            },
            {
                "label": "Run steward",
                "method": "POST",
                "path": "/ai/steward/run",
                "body": {
                    "limit": 100,
                    "max_drafts": 10
                }
            }
        ]
    },
    "agents": {
        "key": "agents",
        "title": "Agent identities",
        "path": "/admin/agents",
        "listKey": null,
        "description": "Every AI agent identity registered in Cairn (automation keys, OAuth/MCP clients, built agents and agent apps), with its scopes, quotas and today's request count.",
        "columns": [
            "name",
            "kind",
            "owner",
            "scopes",
            "enabled",
            "requests_today",
            "last_used_ms"
        ],
        "actions": [
            {
                "label": "Disable",
                "method": "PATCH",
                "path": "/admin/agents/{id}",
                "body": {
                    "enabled": false
                }
            },
            {
                "label": "Pause all agents",
                "method": "POST",
                "path": "/admin/agents/pause",
                "body": {
                    "paused": true
                }
            },
            {
                "label": "Rotate key",
                "method": "POST",
                "path": "/admin/agents/{id}/rotate",
                "body": null
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/admin/agents/{id}",
                "body": null
            }
        ]
    },
    "remote-agents": {
        "key": "remote-agents",
        "title": "Remote ingestion agents",
        "path": "/agents",
        "listKey": null,
        "description": "Ingestion agents installed in customer networks that enrolled with Cairn and pull metadata-ingestion tasks from a pool.",
        "columns": [
            "hostname",
            "pool",
            "status",
            "version",
            "running_tasks",
            "last_heartbeat"
        ],
        "actions": [
            {
                "label": "Revoke",
                "method": "POST",
                "path": "/agents/{agent_id}/revoke",
                "body": null
            },
            {
                "label": "Create pool",
                "method": "POST",
                "path": "/agents/pools",
                "body": {
                    "name": "store-dc-east",
                    "description": "",
                    "allowed_platforms": [],
                    "max_concurrency": 2,
                    "allow_profiling": true
                }
            },
            {
                "label": "New enrollment token",
                "method": "POST",
                "path": "/agents/pools/{pool}/enrollment-tokens?ttl_seconds=3600",
                "body": null
            }
        ]
    },
    "ml-models": {
        "key": "ml-models",
        "title": "ML models",
        "path": "/v1/ml/api/2.0/mlflow/registered-models/search",
        "listKey": "registered_models",
        "description": "Models registered in Cairn's MLflow registry that you may see, with their latest versions and aliases.",
        "columns": [
            "name",
            "user_id",
            "latest_versions",
            "aliases",
            "last_updated_timestamp"
        ],
        "actions": [
            {
                "label": "Turn on monitoring",
                "method": "PUT",
                "path": "/v1/ml/models/{name}/monitor",
                "body": {
                    "baseline": {
                        "table": "features.store_weekly"
                    },
                    "every_minutes": 60,
                    "window_minutes": 1440
                }
            },
            {
                "label": "Run monitoring now",
                "method": "POST",
                "path": "/v1/ml/models/{name}/monitor/run",
                "body": null
            },
            {
                "label": "Stop monitoring",
                "method": "DELETE",
                "path": "/v1/ml/models/{name}/monitor",
                "body": null
            }
        ]
    },
    "ml-deployments": {
        "key": "ml-deployments",
        "title": "Model deployments",
        "path": "/v1/ml/deployments",
        "listKey": null,
        "description": "Registered model versions served from Cairn, scaled with their load (down to zero when idle) and called as the person invoking them.",
        "columns": [
            "name",
            "model",
            "version",
            "replicas",
            "running",
            "in_flight",
            "last_request_ms"
        ],
        "actions": [
            {
                "label": "Predict",
                "method": "POST",
                "path": "/v1/ml/deployments/{name}/invocations",
                "body": {
                    "dataframe_records": [
                        {
                            "store_id": 112,
                            "product_id": 40871,
                            "week": "2026-11-09"
                        }
                    ]
                }
            },
            {
                "label": "Deploy",
                "method": "POST",
                "path": "/v1/ml/deployments",
                "body": {
                    "name": "demand-forecast-prod",
                    "model": "demand-forecast",
                    "version": "6",
                    "min_replicas": 0,
                    "max_replicas": 4,
                    "target_concurrency": 2,
                    "idle_minutes": 10
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/ml/deployments/{name}",
                "body": null
            }
        ]
    },
    "ml-feature-tables": {
        "key": "ml-feature-tables",
        "title": "Feature tables",
        "path": "/v1/ml/feature-tables",
        "listKey": null,
        "description": "Lakehouse tables declared as ML features, with their entity keys and feature columns; reading them is reading the source table under its policies.",
        "columns": [
            "name",
            "table",
            "keys",
            "features",
            "description"
        ],
        "actions": [
            {
                "label": "Serve online",
                "method": "PUT",
                "path": "/v1/ml/feature-tables/{name}/online",
                "body": {
                    "timestamp_column": "updated_at",
                    "ttl_seconds": 86400
                }
            },
            {
                "label": "Sync online now",
                "method": "POST",
                "path": "/v1/ml/feature-tables/{name}/online/sync",
                "body": null
            },
            {
                "label": "Stop serving online",
                "method": "DELETE",
                "path": "/v1/ml/feature-tables/{name}/online",
                "body": null
            },
            {
                "label": "Declare",
                "method": "POST",
                "path": "/v1/ml/feature-tables",
                "body": {
                    "name": "store_weekly_features",
                    "table": "features.store_weekly",
                    "keys": [
                        "store_id"
                    ],
                    "description": "..."
                }
            }
        ]
    },
    "ml-finetunes": {
        "key": "ml-finetunes",
        "title": "Fine-tunes",
        "path": "/v1/ml/finetunes",
        "listKey": "finetunes",
        "description": "Language models fine-tuned inside Cairn on data you may query (PII redacted), usable only by people allowed to read the training data.",
        "columns": [
            "name",
            "base_model",
            "status",
            "examples",
            "created_by",
            "created_ms",
            "finished_ms"
        ],
        "actions": [
            {
                "label": "Generate",
                "method": "POST",
                "path": "/v1/ml/finetunes/{name}/generate",
                "body": {
                    "prompts": [
                        "Ticket: Order arrived with a broken jar\nTeam:"
                    ],
                    "max_new_tokens": 32
                }
            },
            {
                "label": "Start fine-tune",
                "method": "POST",
                "path": "/v1/ml/finetunes",
                "body": {
                    "name": "ticket-triage",
                    "base_model": "distilgpt2",
                    "source": {
                        "connection": "lakehouse",
                        "sql": "SELECT subject, team FROM support.tickets"
                    },
                    "prompt_column": "subject",
                    "completion_column": "team",
                    "prompt_template": "Ticket: {prompt}\nTeam:",
                    "epochs": 3,
                    "learning_rate": 0.0005,
                    "lora": true
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/ml/finetunes/{name}",
                "body": null
            }
        ]
    },
    "access-reviews": {
        "key": "access-reviews",
        "title": "Access reviews",
        "path": "/governance/access-reviews",
        "listKey": null,
        "description": "Campaigns in which data owners confirm or revoke the time-boxed access people hold; undecided items are revoked (or kept) at the due date.",
        "columns": [
            "name",
            "state",
            "due_ms"
        ],
        "actions": [
            {
                "label": "Start campaign",
                "method": "POST",
                "path": "/governance/access-reviews",
                "body": {
                    "name": "Q4 finance access",
                    "personas": [
                        "finance-analyst"
                    ],
                    "reviewers": [
                        "urn:li:corpuser:alice"
                    ],
                    "due_days": 14,
                    "on_no_response": "revoke"
                }
            },
            {
                "label": "Decide item",
                "method": "POST",
                "path": "/governance/access-reviews/{id}/items/{item_id}",
                "body": {
                    "decision": "keep",
                    "comment": "Still on the forecasting project"
                }
            },
            {
                "label": "Close campaign",
                "method": "POST",
                "path": "/governance/access-reviews/{id}/close",
                "body": {}
            }
        ]
    },
    "residency": {
        "key": "residency",
        "title": "Data residency",
        "path": "/governance/residency",
        "listKey": "pins",
        "description": "Read-only view of which lakehouse namespaces are pinned to which region and where their data may be processed or sent; set by deployment configuration, not the API.",
        "columns": [
            "namespaces",
            "region",
            "allow"
        ],
        "actions": []
    },
    "posture": {
        "key": "posture",
        "title": "Security posture",
        "path": "/governance/posture",
        "listKey": "findings",
        "description": "The latest automated scan of Cairn's own configuration for mistakes that expose data, with a 0-100 score and how to fix each finding.",
        "columns": [
            "severity",
            "check",
            "title",
            "fix"
        ],
        "actions": [
            {
                "label": "Scan now",
                "method": "POST",
                "path": "/governance/posture/scan",
                "body": {}
            }
        ]
    },
    "tokens": {
        "key": "tokens",
        "title": "Tokenisation domains",
        "path": "/governance/tokens/domains",
        "listKey": "domains",
        "description": "Kinds of sensitive value (email, IBAN, customer ID) that are replaced by reversible tokens as data lands, with audited detokenisation for allowed people and purposes.",
        "columns": [
            "name",
            "description",
            "created_by",
            "created_ms"
        ],
        "actions": [
            {
                "label": "Create domain",
                "method": "POST",
                "path": "/governance/tokens/domains",
                "body": {
                    "name": "email",
                    "description": "Customer e-mail addresses"
                }
            },
            {
                "label": "Detokenise",
                "method": "POST",
                "path": "/governance/tokens/domains/{name}/detokenize",
                "body": {
                    "tokens": [
                        "tok_abcdefghijklmnopqrstuvwxyz"
                    ],
                    "purpose": "customer support case 4411"
                }
            },
            {
                "label": "Forget (crypto-shred)",
                "method": "POST",
                "path": "/governance/tokens/domains/{name}/forget",
                "body": {
                    "values": [
                        "kim@example.com"
                    ]
                }
            },
            {
                "label": "Rotate master key",
                "method": "POST",
                "path": "/governance/tokens/rotate",
                "body": {}
            }
        ]
    },
    "consent": {
        "key": "consent",
        "title": "Consent purposes",
        "path": "/governance/consent/purposes",
        "listKey": null,
        "description": "Processing purposes (such as marketing) that need a data subject's consent; rows of people who withdrew (opt-out) or never granted (opt-in) are filtered out for the purpose's members.",
        "columns": [
            "name",
            "identifier",
            "mode",
            "enabled",
            "records",
            "filter_state",
            "updated_ms"
        ],
        "actions": [
            {
                "label": "Create purpose",
                "method": "POST",
                "path": "/governance/consent/purposes",
                "body": {
                    "name": "Marketing",
                    "description": "Campaign targeting",
                    "identifier": "email",
                    "mode": "opt_out",
                    "members": {
                        "users": [],
                        "groups": [
                            "urn:li:corpGroup:marketing"
                        ],
                        "personas": []
                    },
                    "enabled": true
                }
            },
            {
                "label": "Record consent",
                "method": "POST",
                "path": "/governance/consent/purposes/{id}/records",
                "body": {
                    "subjects": [
                        {
                            "value": "kim@example.com",
                            "state": "withdrawn"
                        }
                    ],
                    "source": "onetrust"
                }
            },
            {
                "label": "Delete purpose",
                "method": "DELETE",
                "path": "/governance/consent/purposes/{id}",
                "body": null
            }
        ]
    },
    "contracts": {
        "key": "contracts",
        "title": "Data contracts",
        "path": "/quality/contracts",
        "listKey": null,
        "description": "Published promises about a dataset's schema, freshness, volume and quality rules, with whether each is currently met.",
        "columns": [
            "dataset",
            "version",
            "status",
            "owner",
            "checked_ms"
        ],
        "actions": [
            {
                "label": "Publish contract",
                "method": "PUT",
                "path": "/quality/contracts",
                "body": {
                    "dataset": "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.sales.orders,PROD)",
                    "version": "2.1.0",
                    "owner": "urn:li:corpuser:raj",
                    "description": "Orders placed online and in store",
                    "schema": {
                        "columns": [
                            {
                                "name": "order_id",
                                "type": "BIGINT",
                                "required": true
                            },
                            {
                                "name": "store_id",
                                "type": "INTEGER",
                                "required": true
                            },
                            {
                                "name": "total",
                                "type": "DECIMAL(12,2)",
                                "required": false
                            }
                        ],
                        "allow_extra_columns": true
                    },
                    "freshness": {
                        "max_age_hours": 6.0
                    },
                    "quality": []
                }
            },
            {
                "label": "Retire contract",
                "method": "DELETE",
                "path": "/quality/contracts/one?dataset={dataset}",
                "body": null
            },
            {
                "label": "Check proposed schema",
                "method": "POST",
                "path": "/quality/contracts/check",
                "body": {
                    "dataset": "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.sales.orders,PROD)",
                    "columns": {
                        "order_id": {
                            "type": "bigint",
                            "nullable": false
                        },
                        "store_id": {
                            "type": "int",
                            "nullable": false
                        }
                    }
                }
            }
        ]
    },
    "passkeys": {
        "key": "passkeys",
        "title": "Passkeys",
        "path": "/v1/passkeys",
        "listKey": "passkeys",
        "description": "Your own passkeys (security keys or device keys) used to confirm sensitive actions such as detokenising or creating a share.",
        "columns": [
            "name",
            "created_ms",
            "last_used_ms"
        ],
        "actions": [
            {
                "label": "Remove passkey",
                "method": "DELETE",
                "path": "/v1/passkeys/{id}",
                "body": null
            },
            {
                "label": "Registration options",
                "method": "POST",
                "path": "/v1/passkeys/register/options",
                "body": {}
            },
            {
                "label": "Step-up options",
                "method": "POST",
                "path": "/v1/passkeys/step-up/options",
                "body": {}
            }
        ]
    },
    "secret-scopes": {
        "key": "secret-scopes",
        "title": "Secret scopes",
        "path": "/v1/secret-scopes",
        "listKey": null,
        "description": "Encrypted stores of API keys and passwords that notebooks, jobs and pipelines read as the person running them, each with its own access list.",
        "columns": [
            "name",
            "description",
            "owner",
            "permission",
            "created_ms"
        ],
        "actions": [
            {
                "label": "Create scope",
                "method": "POST",
                "path": "/v1/secret-scopes",
                "body": {
                    "name": "payments-gateway",
                    "description": "Adyen API credentials"
                }
            },
            {
                "label": "Put secret",
                "method": "PUT",
                "path": "/v1/secret-scopes/{name}/secrets/{key}",
                "body": {
                    "value": "…"
                }
            },
            {
                "label": "Grant access",
                "method": "PUT",
                "path": "/v1/secret-scopes/{name}/acl",
                "body": {
                    "principal": "urn:li:corpGroup:data-eng",
                    "permission": "READ"
                }
            },
            {
                "label": "Delete scope",
                "method": "DELETE",
                "path": "/v1/secret-scopes/{name}",
                "body": null
            }
        ]
    },
    "siem": {
        "key": "siem",
        "title": "SIEM export",
        "path": "/v1/audit/siem",
        "listKey": null,
        "description": "The sinks that receive Cairn's audit log (Splunk, syslog, HTTP collectors), with how far each has got and any delivery error.",
        "columns": [
            "sink",
            "type",
            "lag",
            "sent",
            "last_ms",
            "failures",
            "error"
        ],
        "actions": [
            {
                "label": "Resend from seq",
                "method": "POST",
                "path": "/v1/audit/siem/{sink}/rewind",
                "body": {
                    "seq": 0
                }
            }
        ]
    },
    "limits": {
        "key": "limits",
        "title": "Request limits",
        "path": "/v1/admin/limits",
        "listKey": null,
        "description": "Requests-per-minute quotas for the tenant and each person, and the IP allowlist that restricts where the API can be reached from.",
        "columns": [
            "tenant_per_minute",
            "user_per_minute",
            "users",
            "ip_allowlist",
            "trusted_proxies"
        ],
        "actions": [
            {
                "label": "Save limits",
                "method": "PUT",
                "path": "/v1/admin/limits",
                "body": {
                    "tenant_per_minute": 6000,
                    "user_per_minute": 600,
                    "users": {
                        "urn:li:corpuser:svc-dbt": 3000
                    },
                    "ip_allowlist": [
                        "10.0.0.0/8",
                        "203.0.113.0/24"
                    ],
                    "trusted_proxies": [
                        "10.42.0.0/16"
                    ]
                }
            }
        ]
    },
    "workspaces": {
        "key": "workspaces",
        "title": "Workspaces",
        "path": "/workspaces",
        "listKey": null,
        "description": "Business units (groups of catalog domains) with their own delegated admins and members.",
        "columns": [
            "name",
            "role",
            "domains"
        ],
        "actions": [
            {
                "label": "Save workspace",
                "method": "PUT",
                "path": "/workspaces/{id}",
                "body": {
                    "id": "stores",
                    "name": "Store Operations",
                    "description": "",
                    "domains": [
                        "urn:li:domain:stores"
                    ],
                    "admins": [
                        "urn:li:corpuser:tom"
                    ],
                    "members": [
                        "urn:li:corpGroup:store-ops"
                    ]
                }
            },
            {
                "label": "Delete workspace",
                "method": "DELETE",
                "path": "/workspaces/{id}",
                "body": null
            }
        ]
    },
    "shares": {
        "key": "shares",
        "title": "Shares",
        "path": "/v1/shares",
        "listKey": "shares",
        "description": "Lakehouse tables shared read-only with an outside organisation, which reads them with any Iceberg REST client until the share expires or is revoked.",
        "columns": [
            "name",
            "recipient",
            "tables",
            "state",
            "expires_at",
            "last_used_at"
        ],
        "actions": [
            {
                "label": "Revoke",
                "method": "POST",
                "path": "/v1/shares/{id}/revoke",
                "body": {}
            }
        ],
        "seconds": [
            "created_at",
            "expires_at",
            "revoked_at",
            "last_used_at"
        ]
    },
    "slos": {
        "key": "slos",
        "title": "Service levels",
        "path": "/marketplace/slos",
        "listKey": null,
        "description": "Service level objectives for data products (percent of freshness, volume or quality checks that pass over a window), with the error budget left.",
        "columns": [
            "name",
            "state",
            "sli",
            "objective",
            "budget_remaining",
            "burn_rate",
            "window_days"
        ],
        "actions": [
            {
                "label": "Save SLO",
                "method": "PUT",
                "path": "/marketplace/slos/{id}",
                "body": {
                    "id": "orders-freshness",
                    "product_urn": "urn:li:dataProduct:sales-orders",
                    "name": "Orders fresh within 6h",
                    "kind": "freshness",
                    "objective": 99.0,
                    "window_days": 28,
                    "description": ""
                }
            },
            {
                "label": "Delete SLO",
                "method": "DELETE",
                "path": "/marketplace/slos/{id}",
                "body": null
            }
        ]
    },
    "ai-usage": {
        "key": "ai-usage",
        "title": "AI usage",
        "path": "/ai/usage",
        "listKey": "groups",
        "description": "Model calls, tokens and estimated cost over recent days, grouped by task, person, model, provider or day, with today's spend against the AI budget.",
        "columns": [
            "key",
            "calls",
            "input_tokens",
            "output_tokens",
            "cost_usd"
        ],
        "actions": []
    },
    "budgets": {
        "key": "budgets",
        "title": "Budgets",
        "path": "/v1/budgets",
        "listKey": null,
        "description": "Spending limits per period for a part of the platform (everything, a domain, owner, platform, tables, a warehouse or AI), with alerts at thresholds and on forecast overruns.",
        "columns": [
            "name",
            "scope",
            "amount_usd",
            "period",
            "spent_usd",
            "enabled",
            "checked_ms"
        ],
        "actions": [
            {
                "label": "Check now",
                "method": "POST",
                "path": "/v1/budgets/{id}/check",
                "body": null
            },
            {
                "label": "Create",
                "method": "POST",
                "path": "/v1/budgets",
                "body": {
                    "name": "AI spend",
                    "amount_usd": 2000,
                    "period": "month",
                    "scope": {
                        "ai": true
                    },
                    "thresholds": [
                        80,
                        100
                    ],
                    "forecast": true,
                    "notify": [
                        "urn:li:corpGroup:finops"
                    ]
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/budgets/{id}",
                "body": null
            }
        ]
    },
    "ai-gateway-models": {
        "key": "ai-gateway-models",
        "title": "Gateway models",
        "path": "/v1/ai/gateway/models",
        "listKey": "data",
        "description": "The language models you may call through Cairn's AI gateway with your Cairn token (calls are metered to you and use Cairn's provider keys).",
        "columns": [
            "object"
        ],
        "actions": []
    },
    "chargeback": {
        "key": "chargeback",
        "title": "Chargeback statements",
        "path": "/v1/chargeback/statements",
        "listKey": "statements",
        "description": "Monthly statements that charge SQL, AI, storage and compute costs to finance cost centres, frozen when issued.",
        "columns": [
            "month",
            "version",
            "total_usd",
            "issued_by",
            "issued_ms"
        ],
        "actions": [
            {
                "label": "Issue statement",
                "method": "POST",
                "path": "/v1/chargeback/statements",
                "body": {
                    "month": "2026-10"
                }
            },
            {
                "label": "Save cost centre",
                "method": "PUT",
                "path": "/v1/chargeback/cost-centres/{code}",
                "body": {
                    "name": "Store Operations",
                    "teams": [
                        "urn:li:corpGroup:store-ops"
                    ]
                }
            },
            {
                "label": "Delete cost centre",
                "method": "DELETE",
                "path": "/v1/chargeback/cost-centres/{code}",
                "body": null
            }
        ]
    },
    "chargeback-cost-centres": {
        "key": "chargeback-cost-centres",
        "title": "Cost centres",
        "path": "/v1/chargeback/cost-centres",
        "listKey": "cost_centres",
        "description": "Finance cost centres and the teams whose platform use they pay for.",
        "columns": [
            "code",
            "name",
            "teams",
            "updated_by",
            "updated_ms"
        ],
        "actions": [
            {
                "label": "Save cost centre",
                "method": "PUT",
                "path": "/v1/chargeback/cost-centres/{code}",
                "body": {
                    "name": "Store Operations",
                    "teams": [
                        "urn:li:corpGroup:store-ops"
                    ]
                }
            },
            {
                "label": "Delete cost centre",
                "method": "DELETE",
                "path": "/v1/chargeback/cost-centres/{code}",
                "body": null
            }
        ]
    }
};

export { HUBS } from './hubs';
