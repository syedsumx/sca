import type { ResourceSpec } from './types';

/**
 * The workspace's platform areas: each hub page is a set of tabs, each tab one backend area shown by
 * ResourceView against the real API (its list endpoint, row details and actions). The paths, list keys and
 * fields were read from the API's routers and services; keep them in step when an endpoint changes.
 */
export const RESOURCE_SPECS: Record<string, ResourceSpec> = {
    "ontology-terms": {
        "key": "ontology-terms",
        "title": "Ontology terms",
        "path": "/v1/ontology/terms",
        "listKey": "terms",
        "description": "Business terms Cairn Ontology compiled from the catalog and query history, each with the columns it maps to (display names, date notations), its synonyms and where it came from. Only terms over datasets you may see.",
        "docs": "administration.html#cairn-ontology-the-ontology-compiler",
        "columns": [
            "name",
            "table_name",
            "synonyms",
            "attributes",
            "provenance"
        ],
        "actions": [
            {
                "label": "Validate SQL",
                "method": "POST",
                "path": "/v1/ontology/validate-sql",
                "body": {
                    "sql": "SELECT c.region, sum(o.amount) FROM retail.orders o JOIN retail.customers c ON o.customer_id = c.id GROUP BY 1"
                }
            }
        ]
    },
    "ontology-relationships": {
        "key": "ontology-relationships",
        "title": "Relationships",
        "path": "/v1/ontology/relationships",
        "listKey": "relationships",
        "description": "How tables join: declared keys, keys the compiler inferred (names, types, values and a model's judgement) and joins seen in the query log, with evidence, confidence and the default join among several roles. Ask-your-data is given these; generated SQL is checked against them.",
        "docs": "administration.html#cairn-ontology-the-ontology-compiler",
        "columns": [
            "source",
            "source_column",
            "target",
            "target_column",
            "cardinality",
            "confidence",
            "provenance",
            "join_default"
        ],
        "actions": []
    },
    "ontology-graph": {
        "key": "ontology-graph",
        "title": "Graph",
        "path": "/v1/ontology/graph",
        "listKey": null,
        "description": "The compiled terms and the relationships between their tables, as a graph (also as JSON-LD or RDF Turtle: format=jsonld or turtle).",
        "docs": "administration.html#cairn-ontology-the-ontology-compiler",
        "columns": [],
        "view": "ontology-graph",
        "actions": []
    },
    "ontology-proposals": {
        "key": "ontology-proposals",
        "title": "Ontology proposals",
        "path": "/v1/ontology/proposals",
        "listKey": "proposals",
        "description": "What a compilation proposes, waiting for review: glossary terms linked to their columns, join relationships and metrics. Accepting needs EDIT_ENTITY_GLOSSARY_TERMS on the datasets: a term is written to the glossary (node Ontology) with its column links; a join or metric becomes an approved definition that Ask-your-data uses.",
        "docs": "administration.html#cairn-ontology-the-ontology-compiler",
        "columns": [
            "title",
            "kind",
            "confidence",
            "status",
            "version"
        ],
        "actions": [
            {
                "label": "Accept",
                "method": "POST",
                "path": "/v1/ontology/proposals/{id}/accept",
                "body": null
            },
            {
                "label": "Reject",
                "method": "POST",
                "path": "/v1/ontology/proposals/{id}/reject",
                "body": null
            }
        ]
    },
    "ontology-runs": {
        "key": "ontology-runs",
        "title": "Compilation runs",
        "path": "/v1/ontology/runs",
        "listKey": "runs",
        "description": "Each compilation, versioned: full or incremental (tables unchanged since the published run are reused), its state and what it built. A cancelled or interrupted run resumes from the tables it had not committed. Compiling needs MANAGE_GLOSSARIES.",
        "docs": "administration.html#cairn-ontology-the-ontology-compiler",
        "columns": [
            "version",
            "state",
            "mode",
            "started_ms",
            "finished_ms",
            "requested_by",
            "stats"
        ],
        "actions": [
            {
                "label": "Compile now",
                "method": "POST",
                "path": "/v1/ontology/runs",
                "body": {
                    "mode": "incremental",
                    "sync": true,
                    "wait": false
                }
            },
            {
                "label": "Sync catalog and queries",
                "method": "POST",
                "path": "/v1/ontology/sync",
                "body": null
            },
            {
                "label": "Cancel",
                "method": "POST",
                "path": "/v1/ontology/runs/{id}/cancel",
                "body": null
            },
            {
                "label": "Resume",
                "method": "POST",
                "path": "/v1/ontology/runs/{id}/resume",
                "body": null
            },
            {
                "label": "Import as proposals",
                "method": "POST",
                "path": "/v1/ontology/runs/{id}/import",
                "body": null
            }
        ]
    },
    "agent-listings": {
        "key": "agent-listings",
        "iac": {"kind": "marketplace_listings", "id": "id"},
        "title": "Marketplace listings",
        "path": "/marketplace/listings",
        "listKey": "listings",
        "description": "Agents (agent apps and custom agents) and skills (installed extensions) offered in the marketplace beside data products. Approval of a request grants USE_AGENT (or USE_SKILL) on that one agent or skill, until it expires.",
        "docs": "administration.html#agents-and-skills-in-the-marketplace",
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
                },
                "fixed": true
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
        "docs": "marketplace.html#sharing-outside-the-company",
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
        "iac": {"kind": "external_catalogs", "id": "name"},
        "title": "External catalogs",
        "path": "/v1/external-catalogs",
        "listKey": "catalogs",
        "rowKey": "name",
        "description": "Other organisations' and cloud warehouses' catalogs (Iceberg REST, Snowflake Open Catalog and Horizon, Databricks Unity Catalog, Snowflake live), queried in Trino as catalogs of their own under the lakehouse's grants, masks and row filters; read-only unless registered writable. Their credentials stay in Cairn.",
        "docs": "administration.html#external-iceberg-catalogs",
        "columns": [
            "name",
            "preset",
            "platform",
            "uri",
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
                },
                "fixed": true
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
                "label": "Connect Snowflake Open Catalog",
                "values": {"preset": "snowflake_open_catalog", "auth": "oauth2"},
                "method": "POST",
                "path": "/v1/external-catalogs",
                "body": {
                    "name": "snowflake",
                    "preset": "snowflake_open_catalog",
                    "uri": "https://myorg-myaccount.snowflakecomputing.com/polaris/api/catalog",
                    "warehouse": "analytics",
                    "client_id": "cairn-service-connection",
                    "credential_ref": "scope:snowflake/client-secret",
                    "scope": "PRINCIPAL_ROLE:ALL"
                }
            },
            {
                "label": "Connect Snowflake (Horizon)",
                "values": {"preset": "snowflake_horizon", "auth": "oauth2"},
                "method": "POST",
                "path": "/v1/external-catalogs",
                "body": {
                    "name": "snowflake_iceberg",
                    "preset": "snowflake_horizon",
                    "uri": "https://myorg-myaccount.snowflakecomputing.com/polaris/api/catalog",
                    "warehouse": "ANALYTICS",
                    "auth": "oauth2",
                    "credential_ref": "scope:snowflake/pat",
                    "scope": "session:role:CAIRN_READER"
                }
            },
            {
                "label": "Connect Databricks Unity Catalog",
                "values": {"preset": "databricks_unity", "auth": "token"},
                "method": "POST",
                "path": "/v1/external-catalogs",
                "body": {
                    "name": "databricks",
                    "preset": "databricks_unity",
                    "uri": "https://dbc-1234.cloud.databricks.com/api/2.1/unity-catalog/iceberg-rest",
                    "warehouse": "main",
                    "auth": "token",
                    "credential_ref": "scope:databricks/pat"
                }
            },
            {
                "label": "Connect Snowflake (live)",
                "values": {"preset": "snowflake_federated", "auth": "password"},
                "method": "POST",
                "path": "/v1/external-catalogs",
                "body": {
                    "name": "snowflake_live",
                    "preset": "snowflake_federated",
                    "uri": "https://myorg-myaccount.snowflakecomputing.com",
                    "database": "ANALYTICS",
                    "warehouse": "REPORTING_WH",
                    "role": "CAIRN_READER",
                    "client_id": "CAIRN_READER_USER",
                    "credential_ref": "scope:snowflake/password"
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
    "external-locations": {
        "key": "external-locations",
        "iac": {"kind": "external_locations", "id": "name"},
        "title": "External locations",
        "path": "/v1/storage/locations",
        "listKey": "locations",
        "rowKey": "name",
        "description": "Places on your own cloud storage (ADLS Gen2, S3, GCS) where governed lakehouse tables may live, each with the storage credential Cairn Catalog reaches it with. Tables outside every location are refused; CREATE_TABLE, READ_FILES and WRITE_FILES are granted to people and groups, and every credential handed out is audited.",
        "docs": "administration.html#storage-credentials-and-external-locations",
        "columns": [
            "name",
            "url",
            "credential",
            "read_only",
            "privileges",
            "owner"
        ],
        "actions": [
            {
                "label": "Grant",
                "method": "PUT",
                "path": "/v1/storage/locations/{name}/grants",
                "body": {
                    "principal": "urn:li:corpGroup:analysts",
                    "privileges": ["CREATE_TABLE", "READ_FILES"]
                }
            },
            {
                "label": "Make read-only",
                "method": "PATCH",
                "path": "/v1/storage/locations/{name}",
                "body": {
                    "read_only": true
                },
                "fixed": true
            },
            {
                "label": "Register",
                "method": "POST",
                "path": "/v1/storage/locations",
                "body": {
                    "name": "contoso-sales",
                    "url": "abfss://sales@contosolake.dfs.core.windows.net/lakehouse",
                    "credential": "lake-adls"
                }
            },
            {
                "label": "Remove",
                "method": "DELETE",
                "path": "/v1/storage/locations/{name}",
                "body": null
            }
        ]
    },
    "storage-credentials": {
        "key": "storage-credentials",
        "iac": {"kind": "storage_credentials", "id": "name"},
        "title": "Storage credentials",
        "path": "/v1/storage/credentials",
        "listKey": "credentials",
        "rowKey": "name",
        "description": "How Cairn Catalog reaches your storage: an Azure storage account (account key, Entra service principal or workload identity), an S3 bucket through an IAM role, or a GCS bucket. Secrets are only ever references (a file, a Kubernetes Secret, Vault, or the workload identity); Cairn never holds them.",
        "docs": "administration.html#storage-credentials-and-external-locations",
        "columns": [
            "name",
            "provider",
            "config",
            "secret_ref",
            "owner",
            "updated_ms"
        ],
        "actions": [
            {
                "label": "Add",
                "method": "POST",
                "path": "/v1/storage/credentials",
                "body": {
                    "name": "lake-adls",
                    "provider": "azure",
                    "config": {
                        "account": "contosolake",
                        "auth": "service_principal",
                        "tenant_id": "contoso-tenant",
                        "client_id": "cairn-catalog"
                    },
                    "secret_ref": "k8s:contoso-lake/client-secret"
                }
            },
            {
                "label": "Change the secret reference",
                "method": "PATCH",
                "path": "/v1/storage/credentials/{name}",
                "body": {
                    "secret_ref": "k8s:contoso-lake/client-secret-2"
                }
            },
            {
                "label": "Remove",
                "method": "DELETE",
                "path": "/v1/storage/credentials/{name}",
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
        "docs": "context-and-ai.html#governed-metrics-semantic-layer",
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
        "iac": {"kind": "mirrors", "id": "name"},
        "title": "Mirrors",
        "path": "/v1/mirrors",
        "listKey": "mirrors",
        "description": "Keeps a lakehouse table in step with a table in an operational database (PostgreSQL, MySQL, MongoDB) by copying its changes as they happen.",
        "docs": "administration.html#mirrors-postgresql-tables-in-the-lakehouse",
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
            },
            {
                "label": "New mirror",
                "method": "POST",
                "path": "/v1/mirrors",
                "body": {}
            }
        ]
    },
    "document-sources": {
        "key": "document-sources",
        "iac": {"kind": "document_sources", "id": "name"},
        "title": "Document sources",
        "path": "/v1/document-sources",
        "listKey": null,
        "description": "Copies files from a SharePoint/OneDrive or Google Drive folder into a store prefix on a schedule, so a files pipe can index them.",
        "docs": "administration.html#document-sources-sharepoint-and-google-drive",
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
                "label": "Edit",
                "method": "PUT",
                "path": "/v1/document-sources/{name}",
                "body": {
                    "name": "handbook",
                    "kind": "google_drive",
                    "location": {
                        "folder_id": "handbook-folder"
                    },
                    "bucket": "landing",
                    "prefix": "handbook/",
                    "secret_scope": "finance-db"
                }
            },
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
            },
            {
                "label": "New document source",
                "method": "POST",
                "path": "/v1/document-sources",
                "body": {
                    "name": "handbook",
                    "kind": "google_drive",
                    "location": {
                        "folder_id": "handbook-folder"
                    },
                    "bucket": "landing",
                    "prefix": "handbook/",
                    "secret_scope": "finance-db"
                }
            }
        ]
    },
    "saas-syncs": {
        "key": "saas-syncs",
        "iac": {"kind": "saas_syncs", "id": "name"},
        "title": "SaaS syncs",
        "path": "/v1/saas-syncs",
        "listKey": null,
        "description": "Loads Salesforce, HubSpot or Google Sheets data into a lakehouse table on a schedule, only the records changed since the last run, and tables shared with you through Delta Sharing (Databricks) at each new version. Lists only your own syncs.",
        "docs": "administration.html#saas-connectors",
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
                "label": "Edit",
                "method": "PUT",
                "path": "/v1/saas-syncs/{name}",
                "body": {
                    "name": "accounts",
                    "kind": "salesforce",
                    "object": "Account",
                    "fields": [
                        "Id",
                        "Name"
                    ],
                    "target": "crm.accounts",
                    "secret_scope": "finance-db"
                }
            },
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
            },
            {
                "label": "New SaaS sync",
                "method": "POST",
                "path": "/v1/saas-syncs",
                "body": {
                    "name": "accounts",
                    "kind": "salesforce",
                    "object": "Account",
                    "fields": [
                        "Id",
                        "Name"
                    ],
                    "target": "crm.accounts",
                    "secret_scope": "finance-db"
                }
            },
            {
                "label": "Read a Delta Sharing table",
                "values": {"kind": "delta_sharing"},
                "method": "POST",
                "path": "/v1/saas-syncs",
                "body": {
                    "name": "shared-orders",
                    "kind": "delta_sharing",
                    "object": "sales_share.sales.orders",
                    "fields": [],
                    "target": "shared.orders",
                    "secret_scope": "databricks-share",
                    "cron": "0 * * * *"
                }
            }
        ]
    },
    "reverse-syncs": {
        "key": "reverse-syncs",
        "iac": {"kind": "reverse_syncs", "id": "name"},
        "title": "Reverse syncs",
        "path": "/v1/reverse-syncs",
        "listKey": null,
        "description": "Sends rows from a lakehouse table out to HubSpot or Salesforce on a schedule, as their owner, sending only new and changed rows. Lists only your own syncs.",
        "docs": "administration.html#reverse-etl",
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
                "label": "Edit",
                "method": "PUT",
                "path": "/v1/reverse-syncs/{name}",
                "body": {
                    "name": "contacts-out",
                    "kind": "hubspot",
                    "source": "marts.contacts",
                    "object": "contacts",
                    "key": {
                        "column": "email",
                        "field": "email"
                    },
                    "mapping": {
                        "name": "firstname"
                    },
                    "secret_scope": "finance-db"
                }
            },
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
            },
            {
                "label": "New reverse sync",
                "method": "POST",
                "path": "/v1/reverse-syncs",
                "body": {
                    "name": "contacts-out",
                    "kind": "hubspot",
                    "source": "marts.contacts",
                    "object": "contacts",
                    "key": {
                        "column": "email",
                        "field": "email"
                    },
                    "mapping": {
                        "name": "firstname"
                    },
                    "secret_scope": "finance-db"
                }
            }
        ]
    },
    "pipes": {
        "key": "pipes",
        "iac": {"kind": "pipes", "id": "id"},
        "title": "Pipes",
        "path": "/v1/pipes",
        "listKey": null,
        "description": "Loads files that land under a store bucket and prefix (CSV, JSON lines, Parquet, Excel, or any documents) into a lakehouse table, each file once.",
        "docs": "administration.html#pipes-automatic-file-loading",
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
                "label": "Edit",
                "method": "PUT",
                "path": "/v1/pipes/{id}",
                "body": {
                    "name": "orders-files",
                    "bucket": "landing",
                    "prefix": "orders/",
                    "format": "csv",
                    "target": "raw.orders",
                    "create_table": true
                }
            },
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
            },
            {
                "label": "New pipe",
                "method": "POST",
                "path": "/v1/pipes",
                "body": {
                    "name": "orders-files",
                    "bucket": "landing",
                    "prefix": "orders/",
                    "format": "csv",
                    "target": "raw.orders",
                    "create_table": true
                }
            }
        ]
    },
    "streams": {
        "key": "streams",
        "iac": {"kind": "streams", "id": "id"},
        "title": "Streams",
        "path": "/v1/streams",
        "listKey": null,
        "description": "Writes messages from a Kafka topic (or rows posted over HTTP) into a lakehouse table continuously, including database change events. A transform (one SELECT over batch) reshapes, casts and filters each micro-batch before it lands; rows it fails on are set aside with the rejects.",
        "docs": "administration.html#streams-kafka-and-change-data-capture",
        "columns": [
            "name",
            "topic",
            "format",
            "mode",
            "target",
            "transform",
            "enabled",
            "status"
        ],
        "actions": [
            {
                "label": "Edit",
                "method": "PUT",
                "path": "/v1/streams/{id}",
                "body": {
                    "name": "customers-cdc",
                    "topic": "pg.public.customers",
                    "format": "debezium",
                    "key": [
                        "id"
                    ],
                    "target": "cdc.customers",
                    "create_table": true
                }
            },
            {
                "label": "New HTTP stream with a transform",
                "method": "POST",
                "path": "/v1/streams",
                "body": {
                    "name": "orders-clean",
                    "format": "http",
                    "target": "sales.orders_clean",
                    "create_table": true,
                    "bad_rows": "quarantine",
                    "transform": "SELECT id, lower(email) AS email, CAST(amount AS DECIMAL(12, 2)) AS amount, CAST(ts AS TIMESTAMP) AS ts FROM batch WHERE status <> 'test'"
                }
            },
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
        "docs": "administration.html#sample-datasets",
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
    "parsed-documents": {
        "key": "parsed-documents",
        "title": "Parsed documents",
        "path": "/v1/documents/parsed",
        "listKey": null,
        "description": "Tables and charts read from PDFs and images as data: tables from the text layer without a model, charts (their labels and series) by the deployment's model, metered as you. Yours only; files pipes do the same with \"ai\": {\"parse\": true}.",
        "docs": "administration.html#document-parsing-tables-and-charts",
        "columns": [
            "name",
            "page_count",
            "tables",
            "charts",
            "model",
            "created_ms"
        ],
        "actions": [
            {
                "label": "Parse a document",
                "method": "POST",
                "path": "/v1/documents/parse",
                "body": {
                    "name": "q3-report.pdf",
                    "content": "<the file, base64-encoded>",
                    "charts": true
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/documents/parsed/{id}",
                "body": null
            }
        ]
    },
    "derived": {
        "key": "derived",
        "iac": {"kind": "derived", "id": "id"},
        "title": "Derived tables",
        "path": "/v1/derived",
        "listKey": null,
        "description": "A SELECT kept as a lakehouse table and refreshed on a schedule or when the tables it reads change, run as its owner. Mode scd2 keeps every version of each key (valid_from, valid_to, is_current), closing changed rows and inserting new versions with one MERGE per refresh.",
        "docs": "administration.html#derived-tables-incremental-refresh",
        "columns": [
            "name",
            "target",
            "mode",
            "layer",
            "kind",
            "every_minutes",
            "fresh",
            "last_run_ms"
        ],
        "actions": [
            {
                "label": "Edit",
                "method": "PUT",
                "path": "/v1/derived/{id}",
                "body": {
                    "name": "revenue",
                    "target": "marts.revenue",
                    "on_change": true,
                    "sql": "SELECT region, sum(amount) AS total FROM raw.orders GROUP BY region"
                }
            },
            {
                "label": "New SCD type 2 table",
                "method": "POST",
                "path": "/v1/derived",
                "body": {
                    "name": "customers_history",
                    "target": "crm.customers_history",
                    "mode": "scd2",
                    "key": [
                        "customer_id"
                    ],
                    "tracked": [
                        "segment",
                        "tier"
                    ],
                    "every_minutes": 60,
                    "layer": "silver",
                    "sql": "SELECT customer_id, name, segment, tier, email FROM lakehouse.crm.customers"
                }
            },
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
    "medallion": {
        "key": "medallion",
        "iac": {"kind": "medallion_layers", "id": "table"},
        "title": "Medallion layers",
        "path": "/v1/medallion",
        "listKey": null,
        "rowKey": "table",
        "description": "Bronze, silver and gold on the tables pipes, streams and derived tables write, tagged in the catalog. Gold needs an owner, a description and a met data contract or passing quality monitors; a gold table that stops qualifying shows why.",
        "docs": "administration.html#medallion-layers",
        "columns": [
            "table",
            "layer",
            "resource",
            "gold_ready",
            "set_by",
            "set_ms"
        ],
        "actions": [
            {
                "label": "Set layer",
                "method": "PUT",
                "path": "/v1/medallion/{table}",
                "body": {
                    "layer": "gold"
                }
            },
            {
                "label": "Remove layer",
                "method": "DELETE",
                "path": "/v1/medallion/{table}",
                "body": null
            },
            {
                "label": "Set a table's layer",
                "method": "PUT",
                "path": "/v1/medallion/{table}",
                "scope": "page",
                "body": {
                    "table": "raw.orders",
                    "layer": "bronze"
                }
            }
        ]
    },
    "parquet-tables": {
        "key": "parquet-tables",
        "iac": {"kind": "parquet_tables", "id": "name"},
        "title": "Registered Parquet",
        "path": "/v1/lakehouse/parquet-tables",
        "listKey": null,
        "description": "Parquet files already in the store registered as Iceberg tables in place, without copying them: schema from the files, partitions from Hive-style paths, new files added as they land. Needs read access to the files.",
        "docs": "administration.html#zero-copy-parquet-files-registered-as-tables-in-place",
        "columns": [
            "name",
            "target",
            "bucket",
            "prefix",
            "files",
            "rows",
            "every_minutes",
            "registered_ms",
            "error"
        ],
        "actions": [
            {
                "label": "Register files",
                "method": "POST",
                "path": "/v1/lakehouse/parquet-tables",
                "body": {
                    "name": "events",
                    "bucket": "landing",
                    "prefix": "exports/events/",
                    "target": "files.events",
                    "every_minutes": 60
                }
            },
            {
                "label": "Add new files",
                "method": "POST",
                "path": "/v1/lakehouse/parquet-tables/{name}/refresh",
                "body": {}
            },
            {
                "label": "Schedule",
                "method": "PUT",
                "path": "/v1/lakehouse/parquet-tables/{name}",
                "body": {
                    "every_minutes": 60,
                    "enabled": true
                }
            },
            {
                "label": "Stop registering",
                "method": "DELETE",
                "path": "/v1/lakehouse/parquet-tables/{name}",
                "body": null
            }
        ]
    },
    "vectors": {
        "key": "vectors",
        "iac": {"kind": "vectors", "id": "id"},
        "title": "Vector indexes",
        "path": "/v1/vectors",
        "listKey": null,
        "description": "Indexes a lakehouse table's text column so it can be searched by meaning, kept up to date as rows change. Ask with sources answers a question only from the index's passages, as you, citing the passages it used (or says it cannot answer).",
        "docs": "administration.html#vector-search-on-lakehouse-data",
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
                "label": "Edit",
                "method": "PUT",
                "path": "/v1/vectors/{id}",
                "body": {
                    "name": "articles",
                    "source": "support.articles",
                    "key": "id",
                    "text": "body",
                    "every_minutes": 60
                }
            },
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
            },
            {
                "label": "New vector index",
                "method": "POST",
                "path": "/v1/vectors",
                "body": {
                    "name": "articles",
                    "source": "support.articles",
                    "key": "id",
                    "text": "body",
                    "every_minutes": 60
                }
            }
        ]
    },
    "volumes": {
        "key": "volumes",
        "iac": {"kind": "volumes", "id": "name"},
        "title": "Volumes",
        "path": "/v1/volumes",
        "listKey": null,
        "description": "Governed folders of files in the store, readable and writable by the people and groups they are granted to.",
        "docs": "administration.html#governed-volumes",
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
            },
            {
                "label": "New volume",
                "method": "POST",
                "path": "/v1/volumes",
                "body": {}
            }
        ]
    },
    "storage": {
        "key": "storage",
        "title": "Storage buckets",
        "path": "/storage/v1/buckets",
        "listKey": null,
        "description": "Buckets in Cairn Store with their size, object count, quota and settings.",
        "docs": "administration.html#cairn-store",
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
        "iac": {"kind": "maintenance", "id": "scope"},
        "title": "Maintenance policies",
        "path": "/maintenance/policies",
        "listKey": null,
        "description": "Schedules that compact small files, expire old snapshots, remove orphan files and keep lakehouse tables clustered, per table, schema or everything (*).",
        "docs": "administration.html#table-maintenance",
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
                "label": "Edit",
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
            },
            {
                "label": "New policy",
                "method": "PUT",
                "path": "/maintenance/policies/{scope}",
                "scope": "page",
                "body": {
                    "scope": "raw",
                    "expire_days": 7
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
        "docs": "administration.html#table-maintenance",
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
        "iac": {"kind": "procedures", "id": "name"},
        "title": "Procedures",
        "path": "/v1/procedures",
        "listKey": null,
        "description": "Named, parameterized SQL scripts run on Trino as whoever calls them. Lists those you own or may call.",
        "docs": "administration.html#functions-in-sql-ai-and-python",
        "columns": [
            "name",
            "owner",
            "connection",
            "description",
            "updated_ms"
        ],
        "actions": [
            {
                "label": "Edit",
                "method": "PUT",
                "path": "/v1/procedures/{name}",
                "body": {
                    "name": "noop",
                    "connection": "lakehouse",
                    "statements": [
                        "SELECT 1"
                    ]
                }
            },
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
            },
            {
                "label": "New procedure",
                "method": "POST",
                "path": "/v1/procedures",
                "body": {
                    "name": "noop",
                    "connection": "lakehouse",
                    "statements": [
                        "SELECT 1"
                    ]
                }
            }
        ]
    },
    "warehouses": {
        "key": "warehouses",
        "title": "Warehouses",
        "path": "/v1/warehouses",
        "listKey": null,
        "description": "The team Trino warehouses you may use, whether each is running, starting or stopped, and how busy it is.",
        "docs": "administration.html#warehouses-a-trino-per-team-stopped-when-idle",
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
    "postgres-instances": {
        "key": "postgres-instances",
        "iac": {"kind": "postgres_instances", "id": "name"},
        "title": "Postgres instances",
        "path": "/v1/databases/instances",
        "listKey": "instances",
        "description": "Managed PostgreSQL instances: a cluster per team or workload with standbys and failover, pooling, continuous backups to Cairn Store, point-in-time restore, health, metrics and slow queries. The server Cairn was given is the instance default.",
        "docs": "administration.html#managed-postgres",
        "columns": [
            "name",
            "state",
            "runtime",
            "version",
            "replicas",
            "host",
            "port"
        ],
        "view": "postgres-instances",
        "actions": [
            {
                "label": "New instance",
                "method": "POST",
                "path": "/v1/databases/instances",
                "body": {
                    "name": "orders-pg",
                    "version": "16",
                    "cpu": 1,
                    "memory_mb": 1024,
                    "storage_gb": 10,
                    "replicas": 1
                }
            }
        ]
    },
    "airflow-deployments": {
        "key": "airflow-deployments",
        "iac": {"kind": "airflow_deployments", "id": "name"},
        "title": "Airflow deployments",
        "path": "/v1/airflow/deployments",
        "listKey": "deployments",
        "description": "Managed Airflow: deployments per team and environment that Cairn runs, with sign-in through Cairn, tasks as the deployment's pipeline user, DAG-only and image deploys, rollback, promotion from dev to prod, hibernation, upgrades with a parse check, health and logs.",
        "docs": "administration.html#managed-airflow",
        "columns": [
            "name",
            "environment",
            "state",
            "airflow_version",
            "team",
            "url"
        ],
        "view": "airflow-deployments",
        "actions": [
            {
                "label": "New deployment",
                "method": "POST",
                "path": "/v1/airflow/deployments",
                "body": {
                    "name": "finance-dev",
                    "environment": "dev",
                    "team": "finance",
                    "promotes_to": "finance-stage",
                    "env": {"REPORT_REGION": "EU"},
                    "secrets": [{"variable": "warehouse_password", "scope": "finance", "key": "warehouse_password"}]
                }
            },
            {
                "label": "Edit",
                "method": "PATCH",
                "path": "/v1/airflow/deployments/{name}",
                "body": {"env": {"REPORT_REGION": "US"}}
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/airflow/deployments/{name}"
            }
        ]
    },
    "nifi-connectors": {
        "key": "nifi-connectors",
        "iac": {"kind": "nifi_deployments", "id": "name"},
        "title": "Connectors (NiFi)",
        "path": "/v1/nifi/deployments",
        "listKey": "deployments",
        "description": "Apache NiFi's 300+ connectors, run and governed by Cairn: deployments per team and environment reached only through Cairn, flows kept in Cairn and deployed by it with secrets from secret scopes, landing in the lakehouse as the flow's owner, each flow a pipeline in the catalog with its lineage and runs.",
        "docs": "administration.html#managed-nifi",
        "columns": [
            "name",
            "environment",
            "state",
            "nifi_version",
            "team"
        ],
        "view": "nifi-connectors",
        "actions": [
            {
                "label": "New deployment",
                "method": "POST",
                "path": "/v1/nifi/deployments",
                "body": {
                    "name": "ingest-dev",
                    "environment": "dev",
                    "team": "data-engineers",
                    "resources": {"cpu": 1, "memory_mb": 4096, "heap_mb": 2048, "storage_gb": 20}
                }
            },
            {
                "label": "Scale",
                "method": "PATCH",
                "path": "/v1/nifi/deployments/{name}",
                "body": {"resources": {"cpu": 2, "memory_mb": 8192, "heap_mb": 4096, "storage_gb": 20}}
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/nifi/deployments/{name}"
            }
        ]
    },
    "nifi-flows": {
        "key": "nifi-flows",
        "iac": {"kind": "nifi_flows", "id": "name"},
        "title": "NiFi flows",
        "path": "/v1/nifi/flows",
        "listKey": "flows",
        "description": "Flows Cairn deploys to a NiFi deployment: a NiFi flow definition, its parameters, sensitive ones from secret scopes, and the lakehouse table it lands in as its owner.",
        "docs": "administration.html#managed-nifi",
        "columns": [
            "name",
            "deployment",
            "state",
            "owner",
            "version"
        ],
        "actions": [
            {
                "label": "New flow",
                "method": "POST",
                "path": "/v1/nifi/flows",
                "body": {
                    "name": "contacts-to-lake",
                    "deployment": "ingest-dev",
                    "definition": {"flowContents": {"name": "contacts", "processors": [], "controllerServices": [],
                                                    "processGroups": [], "connections": []}},
                    "parameters": {"sample.records": "[]"},
                    "secrets": [{"parameter": "crm.password", "scope": "crm", "key": "password"}],
                    "sink": {"type": "lakehouse", "namespace": "crm", "table": "contacts"}
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/nifi/flows/{name}"
            }
        ]
    },
    "databases": {
        "key": "databases",
        "title": "Databases",
        "path": "/v1/databases",
        "listKey": "databases",
        "description": "PostgreSQL databases for applications, made through Cairn, with short-lived personal logins.",
        "docs": "administration.html#operational-databases",
        "columns": [
            "name",
            "instance",
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
                "label": "Branch from a point in time",
                "method": "POST",
                "path": "/v1/databases/{name}/branches",
                "body": {
                    "name": "loyalty_app_monday",
                    "at": "2026-11-09T09:00:00Z"
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
    "serving-tables": {
        "key": "serving-tables",
        "iac": {"kind": "serving_tables", "id": "name"},
        "title": "Serving tables",
        "path": "/v1/serving",
        "listKey": "serving_tables",
        "description": "Lakehouse tables kept as row stores in PostgreSQL for lookups by key in milliseconds, synced from the table's snapshots, every answer governed like a read of the table (masks and row filters applied).",
        "docs": "administration.html#serving-tables-lookups-in-milliseconds-on-governed-tables",
        "columns": [
            "name",
            "table",
            "primary_key",
            "rows",
            "lag_ms",
            "synced_ms",
            "writable",
            "error"
        ],
        "actions": [
            {
                "label": "Sync now",
                "method": "POST",
                "path": "/v1/serving/{name}/sync",
                "body": {
                    "full": false
                }
            },
            {
                "label": "Look up a key",
                "method": "POST",
                "path": "/v1/serving/{name}/lookup",
                "body": {
                    "keys": [
                        {
                            "id": 1
                        }
                    ],
                    "columns": [
                        "id",
                        "region",
                        "balance"
                    ]
                }
            },
            {
                "label": "Write rows",
                "method": "POST",
                "path": "/v1/serving/{name}/rows",
                "body": {
                    "rows": [
                        {
                            "id": 9001,
                            "region": "EU",
                            "balance": "10.50"
                        }
                    ]
                }
            },
            {
                "label": "Drop",
                "method": "DELETE",
                "path": "/v1/serving/{name}",
                "body": null
            },
            {
                "label": "Declare",
                "method": "POST",
                "path": "/v1/serving",
                "body": {
                    "name": "accounts",
                    "table": "sales.accounts",
                    "primary_key": [
                        "id"
                    ],
                    "indexes": [
                        [
                            "region"
                        ]
                    ],
                    "writable": false
                }
            }
        ]
    },
    "jobs": {
        "key": "jobs",
        "iac": {"kind": "jobs", "id": "name"},
        "title": "Jobs",
        "path": "/v1/jobs",
        "listKey": null,
        "description": "Notebooks and Python scripts that run on a schedule as their owner, each in its own container, with their output kept.",
        "docs": "administration.html#scheduled-notebook-and-python-jobs",
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
                "label": "Edit",
                "method": "PUT",
                "path": "/v1/jobs/{name}",
                "body": {
                    "name": "nightly-report",
                    "kind": "python",
                    "code": "print('hello')",
                    "cron": "0 2 * * *"
                }
            },
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
            },
            {
                "label": "New job",
                "method": "POST",
                "path": "/v1/jobs",
                "body": {
                    "name": "nightly-report",
                    "kind": "python",
                    "code": "print('hello')",
                    "cron": "0 2 * * *"
                }
            }
        ]
    },
    "apps": {
        "key": "apps",
        "iac": {"kind": "data_apps", "id": "name"},
        "title": "Data apps",
        "path": "/v1/apps",
        "listKey": null,
        "description": "Streamlit or other web apps served by Cairn at /apps/<name>/, each viewer signed in and governed as themselves. Lists the apps you may open; ask an app with a chat about what it shows, answered with your own access.",
        "docs": "administration.html#data-apps",
        "columns": [
            "name",
            "title",
            "chat",
            "runtime",
            "commit",
            "deployed_ms",
            "running"
        ],
        "actions": [
            {
                "label": "Edit",
                "method": "PUT",
                "path": "/v1/apps/{name}",
                "body": {
                    "name": "sales-app",
                    "url": "https://apps.example.com/sales"
                }
            },
            {
                "label": "Ask about this app",
                "method": "POST",
                "path": "/v1/apps/{name}/chat",
                "body": {
                    "question": "Which region grew the most this month, and which tables does that come from?",
                    "view": {
                        "region": "EU"
                    }
                }
            },
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
            },
            {
                "label": "New app",
                "method": "POST",
                "path": "/v1/apps",
                "body": {
                    "name": "sales-app",
                    "url": "https://apps.example.com/sales"
                }
            }
        ]
    },
    "spark-history": {
        "key": "spark-history",
        "title": "Spark applications",
        "path": "/v1/spark/history/api/v1/applications",
        "listKey": null,
        "description": "Spark applications you ran (all of them for operators), from the Spark History Server.",
        "docs": "administration.html#cairn-catalog",
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
        "docs": "administration.html#code-first-agents-cairn-agents-init-dev-deploy",
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
    "sandbox-runs": {
        "key": "sandbox-runs",
        "title": "Code sandboxes",
        "path": "/v1/sandboxes/runs",
        "listKey": "runs",
        "description": "Python (or SQL over DuckDB) that agents and people ran, each run in a container of its own: no network unless asked for (then only Cairn, as the person, and hosts an operator allows), with CPU, memory and time limits. Runs need RUN_CODE; operators see everyone's.",
        "docs": "administration.html#code-sandboxes-for-agents",
        "columns": [
            "actor",
            "agent",
            "language",
            "status",
            "duration_ms",
            "memory_mb",
            "cpus",
            "egress_bytes",
            "started_ms"
        ],
        "actions": [
            {
                "label": "Run code",
                "method": "POST",
                "path": "/v1/sandboxes/runs",
                "body": {
                    "language": "python",
                    "code": "import json, os, urllib.request\nreq = urllib.request.Request(os.environ['CAIRN_URL'] + '/data/query', method='POST', data=json.dumps({'sql': 'SELECT region, count(*) AS n FROM main.users GROUP BY region', 'connection': 'wh'}).encode(), headers={'Authorization': 'Bearer ' + os.environ['CAIRN_TOKEN'], 'Content-Type': 'application/json'})\nresult = json.load(urllib.request.urlopen(req))['rows']",
                    "files": [],
                    "network": {
                        "cairn": true,
                        "allow": []
                    },
                    "limits": {
                        "timeout_s": 60,
                        "memory_mb": 512,
                        "cpus": 1
                    }
                }
            }
        ]
    },
    "custom-agents": {
        "key": "custom-agents",
        "iac": {"kind": "custom_agents", "id": "name"},
        "title": "Custom agents",
        "path": "/v1/custom-agents",
        "listKey": null,
        "description": "Agents a team builds inside Cairn from instructions and a chosen set of assistant tools; they run as the person who starts them, narrowed to the agent's scopes and tools.",
        "docs": "administration.html#ai-agents",
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
                "label": "Edit",
                "method": "PUT",
                "path": "/v1/custom-agents/{name}",
                "body": {
                    "name": "catalog-helper",
                    "instructions": "Find datasets for people.",
                    "tools": [
                        "search_assets"
                    ]
                }
            },
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
            },
            {
                "label": "New custom agent",
                "method": "POST",
                "path": "/v1/custom-agents",
                "body": {
                    "name": "catalog-helper",
                    "instructions": "Find datasets for people.",
                    "tools": [
                        "search_assets"
                    ]
                }
            }
        ]
    },
    "scheduled-tasks": {
        "key": "scheduled-tasks",
        "title": "Scheduled tasks",
        "path": "/ai/scheduled-tasks",
        "listKey": null,
        "description": "Recurring research the assistant runs for you on a schedule, saving each report as your artifact and optionally queuing it for Slack or email once you approve.",
        "docs": "administration.html#cowork-scheduled-tasks-and-an-approved-outbox",
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
                },
                "fixed": true
            },
            {
                "label": "Resume",
                "method": "PUT",
                "path": "/ai/scheduled-tasks/{id}",
                "body": {
                    "enabled": true
                },
                "fixed": true
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
        "docs": "administration.html#cowork-scheduled-tasks-and-an-approved-outbox",
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
        "docs": "administration.html#ai-agents",
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
                    "outlier_factor": 2
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
        "docs": "administration.html#ai-agents",
        "columns": [
            "severity",
            "agent_name",
            "kind",
            "title",
            "value",
            "baseline"
        ],
        "actions": []
    },
    "agent-intent-denials": {
        "key": "agent-intent-denials",
        "title": "Intent denials",
        "path": "/v1/agent-intents/denials",
        "listKey": null,
        "description": "Agent requests refused because they fell outside the intent their run declared: a write during a read-only task, an export, a table the task does not concern. Administrators and operators see all; people see those made for them or by agents they own.",
        "docs": "administration.html#ai-agents",
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
                },
                "fixed": true
            },
            {
                "label": "Make intents optional",
                "method": "PUT",
                "path": "/v1/agent-intents/settings",
                "body": {
                    "required": false
                },
                "fixed": true
            }
        ]
    },
    "agent-abuse-findings": {
        "key": "agent-abuse-findings",
        "title": "Abuse findings",
        "path": "/v1/agent-abuse/findings",
        "listKey": "findings",
        "description": "Prompt injection and data exfiltration caught on agents' model calls and tools: instructions planted in tool results, secrets, PII dumps and destinations outside the allowlist. Critical findings are blocked, and an agent that collects too many is paused. Administrators and operators see all; owners see their agents'.",
        "docs": "administration.html#ai-agents",
        "columns": [
            "created_ms",
            "severity",
            "detector",
            "agent",
            "source",
            "blocked",
            "summary",
            "status"
        ],
        "actions": [
            {
                "label": "Dismiss",
                "method": "POST",
                "path": "/v1/agent-abuse/findings/{id}/dismiss",
                "body": null
            },
            {
                "label": "Change detection settings",
                "method": "PUT",
                "path": "/v1/agent-abuse/settings",
                "body": {
                    "block_at": "critical",
                    "auto_pause_after": 3,
                    "window_hours": 24,
                    "pii_dump_threshold": 10,
                    "allowed_domains": [
                        "corp.example.com"
                    ]
                }
            }
        ]
    },
    "agent-memories": {
        "key": "agent-memories",
        "title": "Agent memories",
        "path": "/v1/me/agent-memories",
        "listKey": "memories",
        "description": "What agents remember about you across sessions (and your teams' and shared memories), each with the run that wrote it. Memories with values masking protects, credentials or instructions are refused; delete any that are about you.",
        "docs": "administration.html#agent-memory",
        "columns": [
            "content",
            "agent",
            "scope",
            "source",
            "source_run",
            "last_used_ms"
        ],
        "actions": [
            {
                "label": "Tell an agent",
                "method": "POST",
                "path": "/v1/me/agent-memories",
                "body": {
                    "agent": "triage",
                    "content": "I prefer answers as a short table",
                    "scope": "person"
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/me/agent-memories/{id}",
                "body": null
            }
        ]
    },
    "pipeline-agent": {
        "key": "pipeline-agent",
        "title": "Pipeline agent tasks",
        "path": "/v1/pipeline-agent/tasks",
        "listKey": null,
        "description": "Pipelines the agent drafted from a goal in plain words (or fixes for a broken derived table), tried as you without writing anything, and shipped only when you approve.",
        "docs": "administration.html#pipeline-agent",
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
        "docs": "context-and-ai.html#ai-steward",
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
        "iac": {"kind": "agent_identities", "id": "id"},
        "title": "Agent identities",
        "path": "/admin/agents",
        "listKey": null,
        "description": "Every AI agent identity registered in Cairn (automation keys, OAuth/MCP clients, built agents and agent apps), with its scopes, quotas and today's request count.",
        "docs": "administration.html#ai-agents",
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
                },
                "fixed": true
            },
            {
                "label": "Pause all agents",
                "method": "POST",
                "path": "/admin/agents/pause",
                "body": {
                    "paused": true
                },
                "fixed": true
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
            },
            {
                "label": "New agent identity",
                "method": "POST",
                "path": "/admin/agents",
                "body": {
                    "name": "nightly-loader",
                    "scopes": [
                        "read"
                    ]
                }
            }
        ]
    },
    "remote-agents": {
        "key": "remote-agents",
        "title": "Collectors",
        "path": "/agents",
        "listKey": null,
        "description": "Collectors (formerly remote ingestion agents): ingestion agents installed in customer networks that enrolled with Cairn and pull metadata-ingestion tasks from a pool.",
        "docs": "remote-agents.html#set-up-a-pool",
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
        "docs": "administration.html#machine-learning-mlflow-behind-cairn",
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
        "iac": {"kind": "model_deployments", "id": "name"},
        "title": "Model deployments",
        "path": "/v1/ml/deployments",
        "listKey": null,
        "description": "Registered model versions served from Cairn, scaled with their load (down to zero when idle) and called as the person invoking them.",
        "docs": "administration.html#managed-model-serving",
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
                "label": "Edit",
                "method": "PUT",
                "path": "/v1/ml/deployments/{name}",
                "body": {}
            },
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
        "docs": "administration.html#machine-learning-mlflow-behind-cairn",
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
        "docs": "administration.html#fine-tuning-on-governed-data",
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
        "docs": "administration.html#access-reviews",
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
        "docs": "administration.html#data-residency",
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
        "docs": "administration.html#security-posture",
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
        "docs": "administration.html#tokenisation",
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
        "iac": {"kind": "consent_purposes", "id": "id"},
        "title": "Consent rules",
        "path": "/governance/consent/purposes",
        "listKey": null,
        "description": "Consent rules (the API and IaC call them consent purposes; they are not access purposes): processing purposes (such as marketing) that need a data subject's consent; rows of people who withdrew (opt-out) or never granted (opt-in) are filtered out for the purpose's members.",
        "docs": "governance.html#consent",
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
                "label": "Edit",
                "method": "PUT",
                "path": "/governance/consent/purposes/{id}",
                "body": {
                    "name": "Marketing",
                    "identifier": "email",
                    "members": {
                        "groups": [
                            "urn:li:corpGroup:marketing"
                        ]
                    }
                }
            },
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
    "catalog-products": {
        "key": "catalog-products",
        "iac": {
            "kind": "data_products",
            "id": "id"
        },
        "title": "Data products",
        "path": "/catalog/products",
        "listKey": "products",
        "description": "Data products in Cairn Core: a named set of assets with a domain and owners, some of them output ports (what consumers read). They appear in search, lineage and the Marketplace at once.",
        "docs": "administration.html#data-products",
        "rowKey": "id",
        "columns": [
            "name",
            "domain",
            "owners",
            "assets",
            "output_ports"
        ],
        "actions": [
            {
                "label": "New data product",
                "method": "POST",
                "path": "/catalog/products",
                "body": {
                    "id": "orders-360",
                    "name": "Orders 360",
                    "description": "Every order, by region and channel",
                    "domain": "urn:li:domain:sales",
                    "owners": [
                        "urn:li:corpGroup:orders-team"
                    ],
                    "assets": [
                        "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.sales.orders,PROD)"
                    ],
                    "output_ports": [
                        "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.sales.orders,PROD)"
                    ]
                }
            },
            {
                "label": "Edit data product",
                "method": "PUT",
                "path": "/catalog/products/{id}",
                "body": {
                    "name": "Orders 360",
                    "description": "Every order, by region and channel",
                    "domain": "urn:li:domain:sales",
                    "owners": [
                        "urn:li:corpGroup:orders-team"
                    ],
                    "assets": [
                        "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.sales.orders,PROD)"
                    ],
                    "output_ports": [
                        "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.sales.orders,PROD)"
                    ]
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/catalog/products/{id}",
                "body": null
            }
        ]
    },
    "catalog-glossary-nodes": {
        "key": "catalog-glossary-nodes",
        "iac": {"kind": "glossary_nodes", "id": "id"},
        "title": "Term groups",
        "path": "/catalog/glossary/nodes",
        "listKey": "nodes",
        "description": "The business glossary's term groups in Cairn Core, maybe inside one another, with their owners.",
        "docs": "administration.html#glossary-and-domains",
        "rowKey": "id",
        "columns": ["name", "parent", "owners"],
        "actions": [
            {"label": "New term group", "method": "POST", "path": "/catalog/glossary/nodes",
             "body": {"id": "finance", "name": "Finance", "description": "Money in and out", "parent": null,
                      "owners": ["urn:li:corpGroup:finance-team"], "custom_properties": {}}},
            {"label": "Edit term group", "method": "PUT", "path": "/catalog/glossary/nodes/{id}",
             "body": {"name": "Finance", "description": "Money in and out", "parent": null,
                      "owners": ["urn:li:corpGroup:finance-team"], "custom_properties": {}}},
            {"label": "Delete", "method": "DELETE", "path": "/catalog/glossary/nodes/{id}", "body": null}
        ]
    },
    "catalog-glossary-terms": {
        "key": "catalog-glossary-terms",
        "iac": {"kind": "glossary_terms", "id": "id"},
        "title": "Glossary terms",
        "path": "/catalog/glossary/terms",
        "listKey": "terms",
        "description": "The business glossary's terms in Cairn Core: definition, group, related terms, owners and stewards, deprecation.",
        "docs": "administration.html#glossary-and-domains",
        "rowKey": "id",
        "columns": ["name", "parent", "stewards", "deprecated"],
        "actions": [
            {"label": "New term", "method": "POST", "path": "/catalog/glossary/terms",
             "body": {"id": "net-revenue", "name": "Net revenue", "definition": "Revenue after refunds and discounts",
                      "parent": "finance", "is_a": [], "has_a": [], "related": [], "owners": [],
                      "stewards": ["urn:li:corpuser:olga"], "custom_properties": {}, "source_url": null,
                      "deprecated": false, "deprecation_note": ""}},
            {"label": "Edit term", "method": "PUT", "path": "/catalog/glossary/terms/{id}",
             "body": {"name": "Net revenue", "definition": "Revenue after refunds and discounts", "parent": "finance",
                      "is_a": [], "has_a": [], "related": [], "owners": [], "stewards": ["urn:li:corpuser:olga"],
                      "custom_properties": {}, "source_url": null, "deprecated": false, "deprecation_note": ""}},
            {"label": "Propose a change", "method": "POST", "path": "/catalog/glossary/proposals",
             "body": {"id": "net-revenue", "name": "Net revenue", "definition": "Revenue after refunds and discounts",
                      "parent": "finance", "is_a": [], "has_a": [], "related": [], "owners": [],
                      "stewards": ["urn:li:corpuser:olga"], "custom_properties": {}, "source_url": null,
                      "deprecated": false, "deprecation_note": "", "intent": "Why the change is wanted"}},
            {"label": "Delete", "method": "DELETE", "path": "/catalog/glossary/terms/{id}", "body": null}
        ]
    },
    "catalog-domains": {
        "key": "catalog-domains",
        "iac": {"kind": "domains", "id": "id"},
        "title": "Domains",
        "path": "/catalog/domains",
        "listKey": "domains",
        "description": "Domains in Cairn Core, maybe inside one another, with their owners; assets and data products are put in them.",
        "docs": "administration.html#glossary-and-domains",
        "rowKey": "id",
        "columns": ["name", "parent", "owners"],
        "actions": [
            {"label": "New domain", "method": "POST", "path": "/catalog/domains",
             "body": {"id": "customer-success", "name": "Customer success", "description": "Support, onboarding and retention",
                      "parent": null, "owners": ["urn:li:corpGroup:cs-team"]}},
            {"label": "Edit domain", "method": "PUT", "path": "/catalog/domains/{id}",
             "body": {"name": "Customer success", "description": "Support, onboarding and retention", "parent": null,
                      "owners": ["urn:li:corpGroup:cs-team"]}},
            {"label": "Delete", "method": "DELETE", "path": "/catalog/domains/{id}", "body": null}
        ]
    },
    "contracts": {
        "key": "contracts",
        "title": "Data contracts",
        "path": "/quality/contracts",
        "listKey": null,
        "description": "Published promises about a dataset's schema, freshness, volume and quality rules, with whether each is currently met.",
        "docs": "data-quality.html#data-contracts",
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
                        "max_age_hours": 6
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
        "docs": "administration.html#passkeys-and-step-up",
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
        "iac": {"kind": "secret_scopes", "id": "name"},
        "title": "Secret scopes",
        "path": "/v1/secret-scopes",
        "listKey": null,
        "description": "Encrypted stores of API keys and passwords that notebooks, jobs and pipelines read as the person running them, each with its own access list.",
        "docs": "administration.html#secret-scopes",
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
    "egress-rules": {
        "key": "egress-rules",
        "iac": {"kind": "egress_rules", "id": "name"},
        "title": "Network egress",
        "path": "/v1/network/egress-rules",
        "listKey": "rules",
        "description": "Where user code (jobs, notebooks, flows' steps, sandboxes, agent apps) may connect: hosts and CIDRs on their ports, for all workspaces or one. With egress enforced, nothing else but Cairn and the lakehouse is reachable, and every refused attempt is logged by the egress proxy.",
        "docs": "administration.html#egress-rules-for-user-code",
        "columns": [
            "name",
            "workspace",
            "host",
            "cidr",
            "ports",
            "description"
        ],
        "rowKey": "name",
        "actions": [
            {
                "label": "Allow a host",
                "method": "POST",
                "path": "/v1/network/egress-rules",
                "body": {
                    "name": "pypi",
                    "workspace": "*",
                    "host": "pypi.org",
                    "ports": [443],
                    "description": "Python packages"
                }
            },
            {
                "label": "Allow a CIDR",
                "method": "POST",
                "path": "/v1/network/egress-rules",
                "body": {
                    "name": "warehouse-vpc",
                    "workspace": "finance",
                    "cidr": "10.20.0.0/16",
                    "ports": [5432]
                }
            },
            {
                "label": "Check a host",
                "method": "POST",
                "path": "/v1/network/egress-rules/check",
                "body": {
                    "workspace": "*",
                    "host": "pypi.org",
                    "port": 443
                }
            },
            {
                "label": "Delete rule",
                "method": "DELETE",
                "path": "/v1/network/egress-rules/{name}",
                "body": null
            }
        ]
    },
    "dataplanes": {
        "key": "dataplanes",
        "iac": {"kind": "dataplanes", "id": "name"},
        "title": "Data planes",
        "path": "/v1/dataplanes",
        "listKey": null,
        "description": "Compute in customers' clusters (Trino, jobs, Spark) that Cairn reaches only through each data plane's outbound tunnel; the data stays in the customer's storage. A data plane whose tunnel is down refuses queries and jobs.",
        "docs": "administration.html#data-planes",
        "columns": [
            "name",
            "tunnel",
            "connectors_online",
            "connection",
            "description"
        ],
        "rowKey": "name",
        "actions": [
            {
                "label": "Add data plane",
                "method": "POST",
                "path": "/v1/dataplanes",
                "body": {
                    "name": "acme-eu",
                    "description": "Acme's EU cluster",
                    "connection": "acme-trino"
                }
            },
            {
                "label": "Enrolment token",
                "method": "POST",
                "path": "/v1/dataplanes/{name}/enrollment-tokens",
                "body": null
            },
            {
                "label": "Delete data plane",
                "method": "DELETE",
                "path": "/v1/dataplanes/{name}",
                "body": null
            }
        ]
    },
    "workspace-compute": {
        "key": "workspace-compute",
        "iac": {"kind": "workspace_compute", "id": "workspace"},
        "title": "Workspace compute",
        "path": "/v1/compute/workspaces",
        "listKey": "workspaces",
        "description": "Where each workspace's queries and jobs run: on its data plane in the customer's cluster (customer) or on Cairn's managed pool (managed).",
        "docs": "administration.html#data-planes",
        "columns": [
            "workspace",
            "compute",
            "dataplane",
            "updated_by"
        ],
        "rowKey": "workspace",
        "actions": [
            {
                "label": "Set compute",
                "method": "PUT",
                "path": "/v1/compute/workspaces/{workspace}",
                "body": {
                    "compute": "customer",
                    "dataplane": "acme-eu"
                }
            },
            {
                "label": "Clear",
                "method": "DELETE",
                "path": "/v1/compute/workspaces/{workspace}",
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
        "docs": "administration.html#audit-log",
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
        "docs": "administration.html#api-quotas-and-the-ip-allowlist",
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
        "iac": {"kind": "workspaces", "id": "id"},
        "title": "Workspaces",
        "path": "/workspaces",
        "listKey": null,
        "description": "Business units (groups of catalog domains) with their own delegated admins and members.",
        "docs": "governance.html#workspaces",
        "columns": [
            "name",
            "role",
            "domains"
        ],
        "actions": [
            {
                "label": "Edit",
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
            },
            {
                "label": "New workspace",
                "method": "PUT",
                "path": "/workspaces/{id}",
                "scope": "page",
                "body": {
                    "id": "finance",
                    "name": "Finance",
                    "domains": [
                        "urn:li:domain:finance"
                    ],
                    "admins": [
                        "urn:li:corpuser:admin"
                    ]
                }
            }
        ]
    },
    "people": {
        "key": "people",
        "iac": {"kind": "people", "id": "id"},
        "title": "People",
        "path": "/v1/people/users",
        "listKey": "items",
        "description": "Everyone in Cairn Core, where each comes from (SCIM, made in Cairn, single sign-on), their attributes and groups. People made here are configuration; people from SCIM are your identity provider's.",
        "docs": "administration.html#people-and-roles",
        "rowKey": "id",
        "columns": [
            "display_name",
            "id",
            "email",
            "department",
            "source",
            "status"
        ],
        "actions": [
            {
                "label": "New person",
                "method": "POST",
                "path": "/v1/people/users",
                "body": {
                    "id": "dana.lee",
                    "display_name": "Dana Lee",
                    "email": "dana.lee@example.com",
                    "department": "Finance",
                    "country": "DE",
                    "employment_type": "contractor"
                }
            },
            {
                "label": "Edit person",
                "method": "PUT",
                "path": "/v1/people/users/{id}",
                "body": {
                    "display_name": "Dana Lee",
                    "title": "Senior analyst"
                }
            },
            {
                "label": "Deactivate",
                "method": "DELETE",
                "path": "/v1/people/users/{id}",
                "body": null
            },
            {
                "label": "Activate",
                "method": "POST",
                "path": "/v1/people/users/{id}/activate",
                "body": null,
                "fixed": true
            }
        ]
    },
    "people-groups": {
        "key": "people-groups",
        "iac": {"kind": "people_groups", "id": "id"},
        "title": "Groups",
        "path": "/v1/people/groups",
        "listKey": "items",
        "description": "Groups in Cairn Core and where each comes from. Groups made here (native: single sign-on never overwrites their members) are configuration.",
        "docs": "administration.html#people-and-roles",
        "rowKey": "id",
        "columns": [
            "name",
            "id",
            "source",
            "members"
        ],
        "actions": [
            {
                "label": "New group",
                "method": "POST",
                "path": "/v1/people/groups",
                "body": {
                    "id": "finance-analysts",
                    "name": "Finance analysts"
                }
            },
            {
                "label": "Edit group",
                "method": "PUT",
                "path": "/v1/people/groups/{id}",
                "body": {
                    "name": "Finance analysts",
                    "description": "Analysts of the finance domain"
                }
            },
            {
                "label": "Add member",
                "method": "POST",
                "path": "/v1/people/groups/{group}/members",
                "body": {
                    "person": "dana.lee"
                }
            },
            {
                "label": "Delete group",
                "method": "DELETE",
                "path": "/v1/people/groups/{id}",
                "body": null
            }
        ]
    },
    "people-principals": {
        "key": "people-principals",
        "title": "Service accounts & agents",
        "path": "/v1/people/principals",
        "listKey": null,
        "description": "Who acts without a person at the keyboard: agent identities (with their owner and scopes), pipelines' workload identities and the accounts they act as, and Cairn's own accounts.",
        "docs": "administration.html#people-and-roles",
        "columns": [
            "name",
            "type",
            "owner",
            "scopes",
            "enabled"
        ]
    },
    "share-packages": {
        "key": "share-packages",
        "title": "Data packages",
        "path": "/v1/share-packages",
        "listKey": "packages",
        "description": "Each share's data package: the shared tables' descriptions, columns, glossary terms and governed metrics, with sample questions, signed by Cairn for the recipient's agents; and the agent apps attached to the share, which the recipient can ask about the shared tables only.",
        "docs": "marketplace.html#sharing-outside-the-company",
        "columns": [
            "share",
            "recipient",
            "state",
            "version",
            "metrics",
            "agents",
            "published_ms"
        ],
        "actions": [
            {
                "label": "Publish package",
                "method": "PUT",
                "path": "/v1/shares/{share}/package",
                "body": {
                    "metrics": [
                        "avg_price"
                    ],
                    "sample_questions": [
                        "What is our average list price?"
                    ],
                    "notes": "Prices are refreshed nightly."
                }
            },
            {
                "label": "Attach agent",
                "method": "POST",
                "path": "/v1/shares/{share}/agents",
                "body": {
                    "app": "partner-helper"
                }
            }
        ],
        "rowKey": "share_id"
    },
    "shares": {
        "key": "shares",
        "title": "Shares",
        "path": "/v1/shares",
        "listKey": "shares",
        "description": "Lakehouse tables shared read-only with an outside organisation, which reads them with any Iceberg REST client until the share expires or is revoked.",
        "docs": "marketplace.html#sharing-outside-the-company",
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
        "iac": {"kind": "slos", "id": "id"},
        "title": "Data product service levels",
        "path": "/marketplace/slos",
        "listKey": null,
        "description": "Service level objectives for data products (percent of freshness, volume or quality checks that pass over a window), with the error budget left.",
        "docs": "marketplace.html#service-level-objectives",
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
                "label": "Edit",
                "method": "PUT",
                "path": "/marketplace/slos/{id}",
                "body": {
                    "id": "orders-freshness",
                    "product_urn": "urn:li:dataProduct:sales-orders",
                    "name": "Orders fresh within 6h",
                    "kind": "freshness",
                    "objective": 99,
                    "window_days": 28,
                    "description": ""
                }
            },
            {
                "label": "Delete SLO",
                "method": "DELETE",
                "path": "/marketplace/slos/{id}",
                "body": null
            },
            {
                "label": "New SLO",
                "method": "PUT",
                "path": "/marketplace/slos/{id}",
                "scope": "page",
                "body": {}
            }
        ]
    },
    "my-ai-usage": {
        "key": "my-ai-usage",
        "title": "Your usage",
        "path": "/ai/usage?mine=true&group_by=day",
        "listKey": "groups",
        "description": "Your own model calls, tokens and estimated cost, day by day, with today's spend against the AI budget. Everyone may see their own; all usage is on the AI usage tab, for operators.",
        "docs": "context-and-ai.html#usage-cost-and-budgets",
        "columns": [
            "key",
            "calls",
            "input_tokens",
            "output_tokens",
            "cost_usd"
        ],
        "actions": []
    },
    "ai-usage": {
        "key": "ai-usage",
        "title": "AI usage",
        "path": "/ai/usage",
        "listKey": "groups",
        "description": "Model calls, tokens and estimated cost over recent days, grouped by task, person, model, provider or day, with today's spend against the AI budget.",
        "docs": "context-and-ai.html#usage-cost-and-budgets",
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
        "iac": {"kind": "budgets", "id": "id"},
        "title": "Budgets",
        "path": "/v1/budgets",
        "listKey": null,
        "description": "Spending limits per period for a part of the platform (everything, a domain, owner, platform, tables, a warehouse or AI), with alerts at thresholds and on forecast overruns.",
        "docs": "administration.html#cost-budgets",
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
                "label": "Edit",
                "method": "PATCH",
                "path": "/v1/budgets/{id}",
                "body": {
                    "name": "Platform",
                    "amount_usd": 10000,
                    "scope": {
                        "all": true
                    }
                }
            },
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
        "docs": "administration.html#ai-gateway-model-calls-from-code-people-and-agents-write",
        "columns": [
            "object"
        ],
        "actions": []
    },
    "gateway-routes": {
        "key": "gateway-routes",
        "iac": {"kind": "ai_gateway_routes", "id": "name"},
        "title": "Gateway routes",
        "path": "/v1/ai/gateway/routes",
        "listKey": "routes",
        "description": "Virtual model names (cairn-auto and the operators' routes) that send each call to the first fitting model you may use, by cost ceiling, capabilities, input size and latency budget, falling back on provider errors, timeouts and rate limits. Calls are metered on the model that answered.",
        "docs": "administration.html#routes-virtual-models-with-rules-and-fallback",
        "columns": [
            "name",
            "strategy",
            "candidates",
            "max_cost_usd",
            "latency_budget_ms",
            "builtin",
            "updated_ms"
        ],
        "rowKey": "name",
        "actions": [
            {
                "label": "Edit",
                "method": "PUT",
                "path": "/v1/ai/gateway/routes/{name}",
                "body": {
                    "name": "fast",
                    "candidates": [
                        {
                            "model": "test-model"
                        }
                    ]
                }
            },
            {
                "label": "Define a route",
                "method": "PUT",
                "path": "/v1/ai/gateway/routes/{name}",
                "body": {
                    "name": "resilient",
                    "description": "Large model, then the small one on the backup provider",
                    "candidates": [
                        {
                            "model": "claude-sonnet-5-5",
                            "capabilities": [
                                "tools"
                            ],
                            "timeout_s": 30
                        },
                        "claude-haiku-5-5"
                    ],
                    "strategy": "ordered",
                    "max_cost_usd": 0.5,
                    "latency_budget_ms": 60000
                },
                "scope": "page"
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/ai/gateway/routes/{name}",
                "body": null
            }
        ]
    },
    "chargeback": {
        "key": "chargeback",
        "title": "Chargeback statements",
        "path": "/v1/chargeback/statements",
        "listKey": "statements",
        "description": "Monthly statements that charge SQL, AI, storage and compute costs to finance cost centres, frozen when issued.",
        "docs": "administration.html#chargeback-statements",
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
                "label": "Edit cost centre",
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
        "iac": {"kind": "cost_centres", "id": "code"},
        "title": "Cost centres",
        "path": "/v1/chargeback/cost-centres",
        "listKey": "cost_centres",
        "description": "Finance cost centres and the teams whose platform use they pay for.",
        "docs": "administration.html#chargeback-statements",
        "columns": [
            "code",
            "name",
            "teams",
            "updated_by",
            "updated_ms"
        ],
        "actions": [
            {
                "label": "Edit",
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
            },
            {
                "label": "New cost centre",
                "method": "PUT",
                "path": "/v1/chargeback/cost-centres/{code}",
                "scope": "page",
                "body": {
                    "code": "CC-4100",
                    "name": "Finance",
                    "teams": [
                        "urn:li:corpGroup:finance"
                    ]
                }
            }
        ]
    },
    "saved-queries": {
        "key": "saved-queries",
        "iac": {"kind": "queries", "id": "id"},
        "title": "Saved queries",
        "path": "/sql/saved",
        "listKey": null,
        "description": "SQL kept for later, with its chart: each runs on its connection as the person running it. Yours, and those shared with you.",
        "docs": "administration.html#sql-workspace",
        "columns": [
            "name",
            "connection",
            "description",
            "shared",
            "updated_ms"
        ],
        "actions": [
            {
                "label": "Edit",
                "method": "PUT",
                "path": "/sql/saved/{id}",
                "body": {
                    "name": "revenue-by-region",
                    "connection": "lakehouse",
                    "sql": "SELECT 1 AS n"
                }
            },
            {
                "label": "New saved query",
                "method": "POST",
                "path": "/sql/saved",
                "body": {
                    "name": "revenue-by-region",
                    "connection": "lakehouse",
                    "sql": "SELECT 1 AS n"
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/sql/saved/{id}",
                "body": null
            }
        ]
    },
    "quality-monitors": {
        "key": "quality-monitors",
        "iac": {"kind": "quality_monitors", "id": "id"},
        "title": "Quality monitors",
        "path": "/quality/monitors",
        "listKey": null,
        "description": "Checks on a table run on a schedule (freshness, volume, a column metric, the schema, or your own SQL); a failing one opens an incident and tells its owners.",
        "docs": "data-quality.html#monitor-types",
        "columns": [
            "name",
            "type",
            "dataset_urn",
            "severity",
            "enabled"
        ],
        "actions": [
            {
                "label": "Edit",
                "method": "PUT",
                "path": "/quality/monitors/{id}",
                "body": {
                    "name": "orders fresh",
                    "dataset_urn": "urn:li:dataset:(urn:li:dataPlatform:iceberg,raw.orders,PROD)",
                    "type": "freshness",
                    "config": {
                        "max_age_hours": 24
                    }
                }
            },
            {
                "label": "New monitor",
                "method": "POST",
                "path": "/quality/monitors",
                "body": {
                    "name": "orders fresh",
                    "dataset_urn": "urn:li:dataset:(urn:li:dataPlatform:iceberg,raw.orders,PROD)",
                    "type": "freshness",
                    "config": {
                        "max_age_hours": 24
                    }
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/quality/monitors/{id}",
                "body": null
            }
        ]
    },
    "notification-routes": {
        "key": "notification-routes",
        "iac": {"kind": "notification_routes", "id": "id"},
        "title": "Notification routes",
        "path": "/notify/routes",
        "listKey": null,
        "description": "Where Cairn sends events (quality incidents, access requests, job failures…): to Slack, Teams, a webhook, a ticketing tool or a topic, filtered by event type and domain.",
        "docs": "integrations.html#notifications",
        "columns": [
            "id",
            "channel",
            "event_types",
            "domains"
        ],
        "actions": [
            {
                "label": "Edit",
                "method": "PUT",
                "path": "/notify/routes/{id}",
                "body": {
                    "event_types": [
                        "quality.incident"
                    ]
                }
            },
            {
                "label": "New route",
                "method": "PUT",
                "path": "/notify/routes/{id}",
                "scope": "page",
                "body": {
                    "id": "finance-incidents",
                    "event_types": [
                        "quality.incident"
                    ],
                    "channel": "slack",
                    "target": "#finance-data"
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/notify/routes/{id}",
                "body": null
            }
        ]
    },
    "observability-slos": {
        "key": "observability-slos",
        "iac": {
            "kind": "observability_slos",
            "id": "id"
        },
        "title": "Platform SLOs",
        "path": "/v1/observability/slos",
        "listKey": "items",
        "description": "Service level objectives: the share of a component's requests that must be good over a window, with error-budget burn alerts (two windows each) sent to notification routes; a burning SLO opens an incident.",
        "docs": "administration.html#observability",
        "rowKey": "id",
        "columns": [
            "name",
            "service",
            "objective",
            "window",
            "enabled"
        ],
        "actions": [
            {
                "label": "New SLO",
                "method": "POST",
                "path": "/v1/observability/slos",
                "body": {
                    "id": "api-availability",
                    "name": "API availability",
                    "service": "cairn-api",
                    "objective": 99.9,
                    "window": "30d",
                    "indicator": {
                        "type": "ratio",
                        "errors": "sum(rate(traces_span_metrics_calls_total{service_name=\"cairn-api\",span_kind=\"SPAN_KIND_SERVER\",status_code=\"STATUS_CODE_ERROR\"}[$window]))",
                        "total": "sum(rate(traces_span_metrics_calls_total{service_name=\"cairn-api\",span_kind=\"SPAN_KIND_SERVER\"}[$window]))"
                    },
                    "routes": []
                }
            },
            {
                "label": "Edit SLO",
                "method": "PUT",
                "path": "/v1/observability/slos/{id}",
                "body": {
                    "name": "API availability",
                    "service": "cairn-api",
                    "objective": 99.5,
                    "window": "30d",
                    "indicator": {
                        "type": "ratio",
                        "errors": "sum(rate(traces_span_metrics_calls_total{service_name=\"cairn-api\",span_kind=\"SPAN_KIND_SERVER\",status_code=\"STATUS_CODE_ERROR\"}[$window]))",
                        "total": "sum(rate(traces_span_metrics_calls_total{service_name=\"cairn-api\",span_kind=\"SPAN_KIND_SERVER\"}[$window]))"
                    }
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/observability/slos/{id}",
                "body": null
            }
        ]
    },
    "observability-alert-rules": {
        "key": "observability-alert-rules",
        "iac": {
            "kind": "observability_alert_rules",
            "id": "id"
        },
        "title": "Alert rules",
        "path": "/v1/observability/alert-rules",
        "listKey": "items",
        "description": "Conditions on metrics (PromQL) or logs (LogQL): every series a condition returns is an alert, firing after it has held for a while, sent to notification routes and people; critical ones open incidents.",
        "docs": "administration.html#observability",
        "rowKey": "id",
        "columns": [
            "name",
            "language",
            "severity",
            "pending_for",
            "enabled"
        ],
        "actions": [
            {
                "label": "New alert rule",
                "method": "POST",
                "path": "/v1/observability/alert-rules",
                "body": {
                    "id": "store-errors",
                    "name": "Store answers errors",
                    "language": "promql",
                    "severity": "critical",
                    "expr": "sum(rate(traces_span_metrics_calls_total{service_name=\"cairn-store\",status_code=\"STATUS_CODE_ERROR\"}[5m])) > 1",
                    "pending_for": "5m",
                    "summary": "{{ $value }} errors a second",
                    "routes": []
                }
            },
            {
                "label": "Edit alert rule",
                "method": "PUT",
                "path": "/v1/observability/alert-rules/{id}",
                "body": {
                    "name": "Store answers errors",
                    "language": "promql",
                    "severity": "critical",
                    "expr": "sum(rate(traces_span_metrics_calls_total{service_name=\"cairn-store\",status_code=\"STATUS_CODE_ERROR\"}[5m])) > 1",
                    "pending_for": "10m"
                }
            },
            {
                "label": "Test a condition",
                "method": "POST",
                "path": "/v1/observability/alert-rules/test",
                "body": {
                    "language": "promql",
                    "expr": "up == 0"
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/observability/alert-rules/{id}",
                "body": null
            }
        ]
    },
    "observability-silences": {
        "key": "observability-silences",
        "iac": {
            "kind": "observability_silences",
            "id": "id"
        },
        "title": "Silences",
        "path": "/v1/observability/silences",
        "listKey": "items",
        "description": "Alerts whose labels match are not sent and open no incident between two times (maintenance windows).",
        "docs": "administration.html#observability",
        "rowKey": "id",
        "columns": [
            "id",
            "comment",
            "starts_at",
            "ends_at",
            "active"
        ],
        "actions": [
            {
                "label": "New silence",
                "method": "POST",
                "path": "/v1/observability/silences",
                "body": {
                    "id": "store-maintenance",
                    "matchers": [
                        {
                            "name": "service",
                            "value": "cairn-store"
                        }
                    ],
                    "ends_at": "2026-12-01T06:00:00Z",
                    "comment": "storage maintenance"
                }
            },
            {
                "label": "Edit silence",
                "method": "PUT",
                "path": "/v1/observability/silences/{id}",
                "body": {
                    "matchers": [
                        {
                            "name": "service",
                            "value": "cairn-store"
                        }
                    ],
                    "ends_at": "2026-12-01T08:00:00Z",
                    "comment": "storage maintenance, extended"
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/observability/silences/{id}",
                "body": null
            }
        ]
    },
    "observability-data-products": {
        "key": "observability-data-products",
        "iac": {
            "kind": "observability_data_products",
            "id": "id"
        },
        "title": "Data products",
        "path": "/v1/observability/data-products",
        "listKey": "items",
        "description": "A named set of tables and the pipelines that build them, watched together: their runs, their tables' freshness, their SLAs.",
        "docs": "administration.html#data-products-and-slas",
        "rowKey": "id",
        "columns": [
            "name",
            "tables",
            "pipelines"
        ],
        "actions": [
            {
                "label": "New data product",
                "method": "POST",
                "path": "/v1/observability/data-products",
                "body": {
                    "id": "daily-sales",
                    "name": "Daily sales",
                    "description": "",
                    "tables": [
                        "lakehouse.sales.daily_revenue"
                    ],
                    "pipelines": [
                        "airflow:daily_sales"
                    ],
                    "owners": []
                }
            },
            {
                "label": "Edit data product",
                "method": "PUT",
                "path": "/v1/observability/data-products/{id}",
                "body": {
                    "name": "Daily sales",
                    "description": "Revenue per day and region",
                    "tables": [
                        "lakehouse.sales.daily_revenue"
                    ],
                    "pipelines": [
                        "airflow:daily_sales"
                    ],
                    "owners": []
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/observability/data-products/{id}",
                "body": null
            }
        ]
    },
    "observability-pipeline-slas": {
        "key": "observability-pipeline-slas",
        "iac": {
            "kind": "observability_pipeline_slas",
            "id": "id"
        },
        "title": "Pipeline SLAs",
        "path": "/v1/observability/pipeline-slas",
        "listKey": "items",
        "description": "An SLA on a pipeline or a data product: alert when a run fails, when nothing succeeded by a daily deadline, when a run takes too long, when its tables are stale, or when a run is anomalously slow.",
        "docs": "administration.html#data-products-and-slas",
        "rowKey": "id",
        "columns": [
            "name",
            "pipeline",
            "data_product",
            "deadline",
            "severity"
        ],
        "actions": [
            {
                "label": "New SLA",
                "method": "POST",
                "path": "/v1/observability/pipeline-slas",
                "body": {
                    "id": "daily-sales-by-six",
                    "name": "Daily sales by 06:00",
                    "pipeline": "",
                    "data_product": "daily-sales",
                    "on_failure": true,
                    "deadline": "06:00",
                    "timezone": "UTC",
                    "max_duration": "",
                    "freshness": "26h",
                    "anomalies": false,
                    "severity": "critical",
                    "routes": []
                }
            },
            {
                "label": "Edit SLA",
                "method": "PUT",
                "path": "/v1/observability/pipeline-slas/{id}",
                "body": {
                    "name": "Daily sales by 06:30",
                    "pipeline": "",
                    "data_product": "daily-sales",
                    "on_failure": true,
                    "deadline": "06:30",
                    "timezone": "UTC",
                    "max_duration": "",
                    "freshness": "26h",
                    "anomalies": false,
                    "severity": "critical",
                    "routes": []
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/observability/pipeline-slas/{id}",
                "body": null
            }
        ]
    },
    "observability-exporters": {
        "key": "observability-exporters",
        "iac": {
            "kind": "observability_exporters",
            "id": "id"
        },
        "title": "OpenTelemetry export",
        "path": "/v1/observability/exporters",
        "listKey": "items",
        "description": "OTLP destinations the bundled Collector sends traces, metrics and logs to, after its redaction: your own Collector or a vendor, with headers from secret scopes.",
        "docs": "administration.html#opentelemetry-export",
        "rowKey": "id",
        "columns": [
            "name",
            "protocol",
            "endpoint",
            "signals",
            "enabled"
        ],
        "actions": [
            {
                "label": "New destination",
                "method": "POST",
                "path": "/v1/observability/exporters",
                "body": {
                    "id": "customer-collector",
                    "name": "Our Collector",
                    "protocol": "grpc",
                    "endpoint": "otel.example.com:4317",
                    "headers": [],
                    "signals": [
                        "traces",
                        "metrics",
                        "logs"
                    ],
                    "sampling_ratio": 1
                }
            },
            {
                "label": "Edit destination",
                "method": "PUT",
                "path": "/v1/observability/exporters/{id}",
                "body": {
                    "name": "Our Collector",
                    "protocol": "grpc",
                    "endpoint": "otel.example.com:4317",
                    "headers": [],
                    "signals": [
                        "traces",
                        "metrics",
                        "logs"
                    ],
                    "sampling_ratio": 0.5
                }
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/v1/observability/exporters/{id}",
                "body": null
            }
        ]
    },
    "scheduled-exports": {
        "key": "scheduled-exports",
        "iac": {"kind": "report_schedules", "id": "id"},
        "title": "Scheduled exports",
        "path": "/sql/reports",
        "listKey": null,
        "description": "A dashboard or workbook sent as CSV or PDF on a schedule to its owner and recipients, each copy rendered as its recipient (formerly scheduled reports; the API is still /sql/reports).",
        "docs": "administration.html#sql-workspace",
        "columns": [
            "name",
            "target",
            "format",
            "every_minutes",
            "recipients",
            "enabled"
        ],
        "actions": [
            {
                "label": "Edit",
                "method": "PUT",
                "path": "/sql/reports/{id}",
                "body": {"name": "weekly revenue", "target": {"dashboard": "<id>"}, "format": "pdf", "every_minutes": 10080}
            },
            {
                "label": "Send now",
                "method": "POST",
                "path": "/sql/reports/{id}/run",
                "body": {},
                "fixed": true
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/sql/reports/{id}",
                "body": null
            },
            {
                "label": "New scheduled export",
                "method": "POST",
                "path": "/sql/reports",
                "body": {"name": "weekly revenue", "target": {"dashboard": "<id>"}, "format": "pdf", "every_minutes": 10080}
            }
        ]
    },
    "metric-alerts": {
        "key": "metric-alerts",
        "iac": {"kind": "metric_alerts", "id": "id"},
        "title": "Metric alerts",
        "path": "/sql/metric-alerts",
        "listKey": null,
        "description": "Alerts on a governed metric (Semantics › Metrics): a change, a range learned from its history, or a threshold, checked on a schedule as its owner, who and the recipients are told.",
        "docs": "administration.html#sql-workspace",
        "columns": [
            "name",
            "metric",
            "grain",
            "rule",
            "state",
            "every_minutes",
            "enabled"
        ],
        "actions": [
            {
                "label": "Edit",
                "method": "PUT",
                "path": "/sql/metric-alerts/{id}",
                "body": {"name": "revenue drop", "metric": "revenue", "grain": "day", "rule": {"kind": "change", "drop_pct": 20}}
            },
            {
                "label": "Check now",
                "method": "POST",
                "path": "/sql/metric-alerts/{id}/check",
                "body": {},
                "fixed": true
            },
            {
                "label": "Delete",
                "method": "DELETE",
                "path": "/sql/metric-alerts/{id}",
                "body": null
            },
            {
                "label": "New metric alert",
                "method": "POST",
                "path": "/sql/metric-alerts",
                "body": {"name": "revenue drop", "metric": "revenue", "grain": "day", "rule": {"kind": "change", "drop_pct": 20}}
            }
        ]
    },
    "model-monitors": {
        "key": "model-monitors",
        "iac": {"kind": "model_monitors", "id": "model"},
        "title": "Model monitors",
        "path": "/v1/ml/monitors",
        "listKey": "monitors",
        "description": "Drift on a served model's inputs, compared with a baseline table or time window and checked on a schedule; the people it names are told when it drifts.",
        "docs": "administration.html#machine-learning-mlflow-behind-cairn",
        "columns": [
            "model",
            "baseline",
            "drift_threshold",
            "window_minutes",
            "every_minutes",
            "notify"
        ],
        "rowKey": "model",
        "actions": [
            {
                "label": "Edit",
                "method": "PUT",
                "path": "/v1/ml/models/{model}/monitor",
                "body": {"baseline": {"table": "ml.training_rows"}, "drift_threshold": 0.2}
            },
            {
                "label": "Check now",
                "method": "POST",
                "path": "/v1/ml/models/{model}/monitor/run",
                "body": {},
                "fixed": true
            },
            {
                "label": "Stop monitoring",
                "method": "DELETE",
                "path": "/v1/ml/models/{model}/monitor",
                "body": null
            },
            {
                "label": "Monitor a model",
                "method": "PUT",
                "path": "/v1/ml/models/{model}/monitor",
                "scope": "page",
                "body": {"model": "churn", "baseline": {"table": "ml.training_rows"}, "drift_threshold": 0.2}
            }
        ]
    },
    "synthetic": {
        "key": "synthetic",
        "title": "Synthetic data",
        "path": null,
        "listKey": null,
        "description": "A lakehouse table shaped like another (its columns, value distributions and relationships), with none of its sensitive values, for tests and demos. Generated as you, from a table you may read.",
        "docs": "administration.html#synthetic-test-data",
        "notes": "A synthetic table is an ordinary lakehouse table: once generated it is in the Catalog. Generate one here.",
        "columns": [],
        "actions": [
            {
                "label": "Generate synthetic table",
                "method": "POST",
                "path": "/v1/synthetic",
                "body": {"source": "sales.orders", "target": "sandbox.orders_synthetic", "rows": 1000, "replace": false}
            }
        ]
    },
    "app-installs": {
        "key": "app-installs",
        "title": "App packages",
        "path": "/v1/app-installs",
        "listKey": "installs",
        "description": "Data apps packaged from git and installed by others on their own data: the installs you may see, with their package and version. Package, release and install here; the data apps themselves are in the Apps tab.",
        "docs": "administration.html#app-packages-apps-others-install-on-their-own-data",
        "columns": [
            "name",
            "package",
            "version",
            "installed_by"
        ],
        "rowKey": "name",
        "actions": [
            {
                "label": "Upgrade",
                "method": "POST",
                "path": "/v1/app-installs/{name}/upgrade",
                "body": {"version": "1.1.0"}
            },
            {
                "label": "Uninstall",
                "method": "DELETE",
                "path": "/v1/app-installs/{name}",
                "body": null
            },
            {
                "label": "New app package",
                "method": "POST",
                "path": "/v1/app-packages",
                "body": {"name": "churn_explorer", "git": {"url": "https://git.example.com/apps/churn.git", "branch": "main", "path": "app"}}
            },
            {
                "label": "Release a version",
                "method": "POST",
                "path": "/v1/app-packages/{name}/versions",
                "scope": "page",
                "body": {"version": "1.0.0"}
            },
            {
                "label": "Install",
                "method": "POST",
                "path": "/v1/app-installs",
                "body": {"name": "churn_finance", "package": "churn_explorer", "version": "1.0.0", "references": {}}
            }
        ]
    },
    "clean-rooms": {
        "key": "clean-rooms",
        "title": "Clean rooms",
        "path": null,
        "listKey": null,
        "description": "Two or more parties join their tables in aggregate, and none of them sees the others' rows: answers only through templates every party approved, and only for groups of at least min_group.",
        "docs": "administration.html#cairn-catalog",
        "notes": "Open a clean room you are a party of by its name, or make one.",
        "columns": [],
        "actions": [
            {
                "label": "New clean room",
                "method": "POST",
                "path": "/v1/clean-rooms",
                "body": {"name": "ad_overlap", "parties": ["urn:li:corpuser:ana", "partner:acme"], "min_group": 10}
            },
            {
                "label": "Bring a table",
                "method": "POST",
                "path": "/v1/clean-rooms/{name}/tables",
                "scope": "page",
                "body": {"table": "crm.customers"}
            },
            {
                "label": "Propose a template",
                "method": "PUT",
                "path": "/v1/clean-rooms/{name}/templates/{tname}",
                "scope": "page",
                "body": {"sql": "SELECT region, count(*) AS n FROM a JOIN b USING (email_hash) GROUP BY region"}
            }
        ]
    },
    "publications": {
        "key": "publications",
        "title": "Publications",
        "path": "/marketplace/publications",
        "listKey": null,
        "description": "Data products published outside Cairn as Snowflake listings or Databricks Delta Sharing shares, and whether each still matches its product. A product's owners publish from its Distribution tab.",
        "docs": "marketplace.html#sharing-outside-the-company",
        "columns": [
            "product",
            "target",
            "share",
            "tables",
            "in_sync",
            "published_by",
            "published_ms"
        ]},
    "audit": {
        "key": "audit",
        "title": "Audit log",
        "path": "/governance/audit?limit=1000",
        "listKey": "entries",
        "description": "The newest 1,000 changes and decisions Cairn recorded, in a tamper-evident hash chain. For everything, GET /governance/audit/export streams the whole log (and SIEM export sends it); GET /governance/audit/verify checks the chain.",
        "docs": "administration.html#audit-log",
        "columns": [
            "ts",
            "category",
            "action",
            "actor",
            "subject"
        ],
        "rowKey": "id"
    }
};

export { HUBS, PAGE_AREAS } from './hubs';
