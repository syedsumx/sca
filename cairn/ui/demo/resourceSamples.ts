/* Sample answers for the platform areas (src/consumer/resources/catalog.ts), in the API's own shapes; fictional data. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const RESOURCE_SAMPLES: Record<string, any> = {
 "/marketplace/listings": {
  "listings": [
   {
    "urn": "urn:li:cairnListing:lst_4f2a9c1e7b3d5a60",
    "id": "lst_4f2a9c1e7b3d5a60",
    "kind": "agent",
    "name": "Ticket triage",
    "target": "triage",
    "target_urn": "urn:li:cairnAgent:triage",
    "source": "agent_app",
    "description": "Sorts the support queue by urgency and drafts first replies.",
    "tags": ["support"],
    "owners": ["urn:li:corpuser:ana"],
    "published": true,
    "consumers": 12,
    "access": {"status": "GRANTED", "request_id": "ar_8d1e0f2a3b4c5d6e", "expires_at": "2026-12-08T09:00:00+00:00"},
    "request_policy": {"auto_approve": false, "max_duration_days": 90,
                       "purposes": ["Analytics", "Reporting", "Data science", "Operations"],
                       "approvers": ["urn:li:corpuser:ana"], "requires_justification": true,
                       "grants": ["USE_AGENT on urn:li:cairnAgent:triage"]},
    "created_ms": 1794096400000,
    "updated_ms": 1794096400000
   },
   {
    "urn": "urn:li:cairnListing:lst_0b9e8d7c6a5f4e31",
    "id": "lst_0b9e8d7c6a5f4e31",
    "kind": "skill",
    "name": "PII scanner",
    "target": "pii-scanner",
    "target_urn": "urn:li:cairnSkill:pii-scanner",
    "source": "extension",
    "description": "Finds personal data in free-text columns.",
    "tags": ["privacy"],
    "owners": ["urn:li:corpuser:privacy-office"],
    "published": true,
    "consumers": 4,
    "access": {"status": "NONE", "request_id": null, "expires_at": null},
    "request_policy": {"auto_approve": false, "max_duration_days": 30,
                       "purposes": ["Analytics", "Operations"],
                       "approvers": ["urn:li:corpGroup:privacy"], "requires_justification": true,
                       "grants": ["USE_SKILL on urn:li:cairnSkill:pii-scanner"]},
    "created_ms": 1793922400000,
    "updated_ms": 1793922400000
   }
  ]
 },
 "/v1/shares/usage/overview": {
  "shares": [
   {
    "id": "8c1f0a5e2b7d4c39a6e0f1b2c3d4e5f6",
    "name": "partner-a",
    "recipient": "Partner A (data@partner.example)",
    "state": "active",
    "created_by": "urn:li:corpuser:ana",
    "expires_at": 1795392400,
    "last_access_ms": 1794182800000,
    "requests": 1284,
    "table_loads": 96,
    "refusals": 3,
    "failures": 0,
    "tables_read": ["analytics.products", "analytics.orders"],
    "unused": false,
    "expiring_soon": false,
    "quality_failing": [
     {"table": "analytics.orders", "urn": "urn:li:dataset:(urn:li:dataPlatform:iceberg,analytics.orders,PROD)", "status": "FAILING"}
    ]
   },
   {
    "id": "2d4e6f8a0b1c4d3e9f7a5b3c1d0e2f4a",
    "name": "auditor-2026",
    "recipient": "External auditor",
    "state": "active",
    "created_by": "urn:li:corpuser:finance-ops",
    "expires_at": 1794355600,
    "last_access_ms": null,
    "requests": 0,
    "table_loads": 0,
    "refusals": 0,
    "failures": 0,
    "tables_read": [],
    "unused": true,
    "expiring_soon": true,
    "quality_failing": []
   }
  ],
  "unused": ["auditor-2026"],
  "expiring_soon": ["auditor-2026"],
  "quality_failing": [
   {"share": "partner-a", "recipient": "Partner A (data@partner.example)", "tables": [
    {"table": "analytics.orders", "urn": "urn:li:dataset:(urn:li:dataPlatform:iceberg,analytics.orders,PROD)", "status": "FAILING"}]}
  ],
  "unused_days": 30,
  "expiring_days": 14
 },
 "/v1/external-catalogs": {
  "catalogs": [
   {
    "name": "partner",
    "uri": "https://catalog.partner.example/iceberg",
    "warehouse": "shared",
    "auth": "oauth2",
    "client_id": "cairn-at-partner",
    "credential_ref": "scope:partner-catalog/client-secret",
    "token_url": "https://login.partner.example/oauth/token",
    "oauth_scope": null,
    "store_endpoint": null,
    "store_region": "eu-west-1",
    "writable": false,
    "description": "Partner order feed",
    "created_by": "urn:li:corpuser:ops",
    "created_ms": 1794096400000,
    "updated_ms": 1794182800000,
    "trino_state": "attached",
    "trino_error": null,
    "last_sync_ms": 1794182800000,
    "last_sync": {"catalog": "partner", "namespaces": 2, "tables": 14, "columns": 121, "removed": 0, "seconds": 1.8},
    "platform": "iceberg"
   },
   {
    "name": "glue_eu",
    "uri": "https://glue.eu-west-1.amazonaws.com/iceberg",
    "warehouse": "123456789012",
    "auth": "token",
    "client_id": null,
    "credential_ref": "env:CAIRN_EXTCAT_GLUE_EU",
    "token_url": null,
    "oauth_scope": null,
    "store_endpoint": null,
    "store_region": "eu-west-1",
    "writable": false,
    "description": null,
    "created_by": "urn:li:corpuser:ops",
    "created_ms": 1793922400000,
    "updated_ms": 1793922400000,
    "trino_state": "failed",
    "trino_error": "Trino could not be reached",
    "last_sync_ms": 1793922400000,
    "last_sync": {"error": "the catalog answered HTTP 403 for namespaces"},
    "platform": "iceberg"
   }
  ]
 },
 "/sql/semantic-models": [
  {
   "id": "5b2e8f0a3c7d41e69a0b4c2d8e1f7a63",
   "connection": "lakehouse",
   "tables": [
    "sales.orders",
    "sales.customers"
   ],
   "status": "draft",
   "created_ms": 1794096400000
  },
  {
   "id": "f80c1d4e6a2b4973b5e0d7a9c3f12b84",
   "connection": "lakehouse",
   "tables": [
    "inventory.stock_levels",
    "inventory.stores",
    "inventory.products"
   ],
   "status": "accepted",
   "created_ms": 1793322400000
  },
  {
   "id": "26d9a3e7b0c54f18a2e6c4b9d07f3e51",
   "connection": "lakehouse",
   "tables": [
    "returns.return_lines"
   ],
   "status": "draft",
   "created_ms": 1792804000000
  }
 ],
 "/v1/mirrors": {
  "mirrors": [
   {
    "name": "orders_pg",
    "connection": "northwind-orders-db",
    "table": "public.orders",
    "target": "sales.orders",
    "key": [
     "order_id"
    ],
    "enabled": true,
    "status": "running",
    "error": null,
    "rows": 1284113,
    "last_lsn": "0/3A7F21C8",
    "last_commit_ms": 1794099958000,
    "created_by": "urn:li:corpuser:alice",
    "created_ms": 1790644000000
   },
   {
    "name": "inventory_mysql",
    "connection": "northwind-warehouse-mysql",
    "table": "wms.stock_levels",
    "target": "supply.stock_levels",
    "key": [
     "sku",
     "warehouse_id"
    ],
    "enabled": false,
    "status": "paused",
    "error": null,
    "rows": 58210,
    "last_lsn": "mysql-bin.000412:88231",
    "last_commit_ms": 1794082000000,
    "created_by": "urn:li:corpuser:raj",
    "created_ms": 1793063200000
   },
   {
    "name": "customers_pg",
    "connection": "northwind-crm-db",
    "table": "public.customers",
    "target": "crm.customers",
    "key": [
     "customer_id"
    ],
    "enabled": true,
    "status": "failed",
    "error": "replication slot cairn_customers_pg was dropped on the server",
    "rows": 402771,
    "last_lsn": "0/19C0B110",
    "last_commit_ms": 1794006400000,
    "created_by": "urn:li:corpuser:mei",
    "created_ms": 1786324000000
   }
  ]
 },
 "/v1/document-sources": [
  {
   "id": "a1f09c2e7b4d4e11",
   "name": "contracts",
   "kind": "sharepoint",
   "location": {
    "drive_id": "b!Xk2pQ9nW-legal",
    "folder": "Contracts/2026"
   },
   "bucket": "documents",
   "prefix": "sharepoint/contracts/",
   "secret_scope": "graph-keys",
   "cron": "0 * * * *",
   "enabled": true,
   "owner": "urn:li:corpuser:alice",
   "status": "ok",
   "error": null,
   "files": 412,
   "last_run_ms": 1794098500000,
   "next_run_ms": 1794102100000,
   "created_ms": 1791508000000
  },
  {
   "id": "c73b1e90d2a84f52",
   "name": "supplier-specs",
   "kind": "google_drive",
   "location": {
    "folder_id": "1qZ8vR3kT0mXyNorthwindSpecs"
   },
   "bucket": "documents",
   "prefix": "gdrive/supplier-specs/",
   "secret_scope": "gdrive-sa",
   "cron": "30 2 * * *",
   "enabled": true,
   "owner": "urn:li:corpuser:raj",
   "status": "failed",
   "error": "Google Drive answered 403: the folder is not shared with the service account",
   "files": 0,
   "last_run_ms": 1794067600000,
   "next_run_ms": 1794154000000,
   "created_ms": 1793840800000
  },
  {
   "id": "5e2d8f41a0c94b77",
   "name": "store-ops-handbook",
   "kind": "sharepoint",
   "location": {
    "drive_id": "b!Rt7oops-StoreOps",
    "folder": ""
   },
   "bucket": "documents",
   "prefix": "sharepoint/store-ops/",
   "secret_scope": "graph-keys",
   "cron": null,
   "enabled": true,
   "owner": "urn:li:corpuser:mei",
   "status": "new",
   "error": null,
   "files": 0,
   "last_run_ms": null,
   "next_run_ms": null,
   "created_ms": 1794099400000
  }
 ],
 "/v1/saas-syncs": [
  {
   "id": "0d6f3a2b9c1e4f80",
   "name": "sf-accounts",
   "owner": "urn:li:corpuser:alice",
   "kind": "salesforce",
   "object": "Account",
   "fields": [
    "Id",
    "Name",
    "Industry",
    "AnnualRevenue",
    "OwnerId"
   ],
   "target": "crm.accounts",
   "secret_scope": "salesforce-keys",
   "cron": "0 * * * *",
   "enabled": true,
   "cursor": "2026-11-07T22:41:09.000+0000",
   "next_run_ms": 1794101200000,
   "running": false,
   "last_run_ms": 1794097600000,
   "last_status": "ok",
   "last_rows": 214,
   "last_error": null,
   "created_ms": 1788916000000
  },
  {
   "id": "8b14e7c05a2d4c39",
   "name": "hubspot-contacts",
   "owner": "urn:li:corpuser:alice",
   "kind": "hubspot",
   "object": "contacts",
   "fields": [
    "email",
    "firstname",
    "lastname",
    "lifecyclestage"
   ],
   "target": "marketing.contacts",
   "secret_scope": "hubspot-keys",
   "cron": "15 */4 * * *",
   "enabled": true,
   "cursor": "1794098100000",
   "next_run_ms": 1794105400000,
   "running": true,
   "last_run_ms": 1794085600000,
   "last_status": "failed",
   "last_rows": 0,
   "last_error": "HubSpot answered 401: the private app token was revoked",
   "created_ms": 1792285600000
  },
  {
   "id": "f2a9c84d71be4e06",
   "name": "store-targets-sheet",
   "owner": "urn:li:corpuser:alice",
   "kind": "google_sheets",
   "object": "1H9xNorthwindTargets/Q4 targets",
   "fields": [
    "store_id",
    "month",
    "target_usd"
   ],
   "target": "finance.store_targets",
   "secret_scope": "gsheets-sa",
   "cron": "0 6 * * 1",
   "enabled": false,
   "cursor": null,
   "next_run_ms": null,
   "running": false,
   "last_run_ms": 1793581600000,
   "last_status": "ok",
   "last_rows": 144,
   "last_error": null,
   "created_ms": 1790212000000
  }
 ],
 "/v1/reverse-syncs": [
  {
   "id": "3c8e1f7a2b904d65",
   "name": "vip-segment-to-hubspot",
   "owner": "urn:li:corpuser:mei",
   "source": "marketing.vip_customers",
   "kind": "hubspot",
   "object": "contacts",
   "key_column": "email",
   "key_field": "email",
   "mapping": {
    "lifetime_value": "northwind_ltv",
    "tier": "northwind_tier"
   },
   "secret_scope": "hubspot-keys",
   "cron": "0 3 * * *",
   "enabled": true,
   "next_run_ms": 1794125200000,
   "running": false,
   "last_run_ms": 1794038800000,
   "last_status": "ok",
   "last_sent": 382,
   "last_failed": 0,
   "last_error": null,
   "created_ms": 1792890400000,
   "region": "eu"
  },
  {
   "id": "9a07d5e3c61f4b28",
   "name": "churn-scores-to-sf",
   "owner": "urn:li:corpuser:mei",
   "source": "analytics.churn_scores",
   "kind": "salesforce",
   "object": "Contact",
   "key_column": "customer_id",
   "key_field": "Northwind_Customer_Id__c",
   "mapping": {
    "churn_risk": "Churn_Risk__c"
   },
   "secret_scope": "salesforce-keys",
   "cron": "30 5 * * *",
   "enabled": true,
   "next_run_ms": 1794132400000,
   "running": false,
   "last_run_ms": 1794046000000,
   "last_status": "failed",
   "last_sent": 1204,
   "last_failed": 17,
   "last_error": "17 records were refused: INVALID_FIELD_FOR_INSERT_UPDATE",
   "created_ms": 1791248800000,
   "region": null
  },
  {
   "id": "e41b0c9f8d2a4a13",
   "name": "store-managers-to-hubspot",
   "owner": "urn:li:corpuser:mei",
   "source": "ops.store_managers",
   "kind": "hubspot",
   "object": "contacts",
   "key_column": "work_email",
   "key_field": "email",
   "mapping": {
    "store_id": "northwind_store"
   },
   "secret_scope": "hubspot-keys",
   "cron": null,
   "enabled": false,
   "next_run_ms": null,
   "running": false,
   "last_run_ms": null,
   "last_status": null,
   "last_sent": null,
   "last_failed": null,
   "last_error": null,
   "created_ms": 1793927200000,
   "region": null
  }
 ],
 "/v1/pipes": [
  {
   "id": "6f1c2e9a0b7d4c83a2e5f0d19b4c7a61",
   "name": "pos-daily-exports",
   "bucket": "landing",
   "prefix": "pos/daily/",
   "format": "csv",
   "target": "sales.pos_transactions",
   "create_table": true,
   "enabled": true,
   "created_by": "urn:li:corpuser:raj",
   "created_ms": 1783732000000,
   "bad_rows": "quarantine",
   "tokenize": {
    "card_number": "pan"
   },
   "ai": {}
  },
  {
   "id": "b3a90d4e17c24f6e8a0c5b2d91e7f384",
   "name": "supplier-invoices",
   "bucket": "documents",
   "prefix": "gdrive/supplier-specs/",
   "format": "files",
   "target": "procurement.supplier_documents",
   "create_table": true,
   "enabled": true,
   "created_by": "urn:li:corpuser:alice",
   "created_ms": 1793322400000,
   "bad_rows": "fail",
   "tokenize": {},
   "ai": {
    "describe": true,
    "extract": {
     "invoice_total": "the invoice total in USD"
    }
   }
  },
  {
   "id": "0e7b5c3a9d2f41e6b8c4a7f0e1d2c3b4",
   "name": "web-clickstream",
   "bucket": "landing",
   "prefix": "web/events/",
   "format": "parquet",
   "target": "web.events",
   "create_table": false,
   "enabled": false,
   "created_by": "urn:li:corpuser:tom",
   "created_ms": 1776820000000,
   "bad_rows": "fail",
   "tokenize": {},
   "ai": {}
  }
 ],
 "/v1/streams": [
  {
   "id": "2b8f0e1c4d6a4a7e9c3b5d0f7e1a2c48",
   "name": "orders-cdc",
   "topic": "northwind.public.orders",
   "format": "debezium",
   "mode": "upsert",
   "key": [
    "order_id"
   ],
   "target": "sales.orders_live",
   "create_table": true,
   "evolve": true,
   "encoding": "avro",
   "start": "earliest",
   "source": "urn:li:dataset:(urn:li:dataPlatform:postgres,orders.public.orders,PROD)",
   "batch_rows": 5000,
   "batch_seconds": 10,
   "enabled": true,
   "created_by": "urn:li:corpuser:raj",
   "created_ms": 1789780000000,
   "bad_rows": "quarantine",
   "tokenize": {},
   "transform": null,
   "status": {
    "status": "running",
    "rows": 9821334,
    "lag": 112,
    "last_commit_ms": 1794099992000,
    "error": null,
    "updated_ms": 1794099995000
   }
  },
  {
   "id": "7c4e2a9b1f0d4e3c8a6b5d2e9f1c0a37",
   "name": "store-footfall",
   "topic": "store.sensors.footfall",
   "format": "json",
   "mode": "append",
   "key": [],
   "target": "ops.footfall",
   "create_table": true,
   "evolve": false,
   "encoding": "json",
   "start": "latest",
   "source": null,
   "batch_rows": 10000,
   "batch_seconds": 30,
   "enabled": true,
   "created_by": "urn:li:corpuser:tom",
   "created_ms": 1792804000000,
   "bad_rows": "fail",
   "tokenize": {},
   "transform": "SELECT store_id, sensor, CAST(visitors AS INTEGER) AS visitors, CAST(ts AS TIMESTAMP) AS ts FROM batch WHERE sensor <> 'test'",
   "status": {
    "status": "error",
    "rows": 412900,
    "lag": 58211,
    "last_commit_ms": 1794096400000,
    "error": "column visitors: expected integer, got \"n/a\"",
    "updated_ms": 1794099940000
   }
  },
  {
   "id": "d91a3f7e5c2b4b08a4e6c1f9b7d3e520",
   "name": "pos-http",
   "topic": "http",
   "format": "http",
   "mode": "append",
   "key": [],
   "target": "sales.pos_live",
   "create_table": true,
   "evolve": false,
   "encoding": "json",
   "start": "earliest",
   "source": null,
   "batch_rows": 5000,
   "batch_seconds": 10,
   "enabled": true,
   "created_by": "urn:li:corpuser:raj",
   "created_ms": 1793754400000,
   "bad_rows": "fail",
   "tokenize": {
    "card_number": "pan"
   },
   "transform": "SELECT receipt_id, store_id, card_number, CAST(total AS DECIMAL(12, 2)) AS total, upper(currency) AS currency, CAST(sold_at AS TIMESTAMP) AS sold_at FROM batch WHERE total IS NOT NULL",
   "status": {
    "status": null,
    "rows": null,
    "lag": null,
    "last_commit_ms": null,
    "error": null,
    "updated_ms": null
   }
  }
 ],
 "/v1/samples": [
  {
   "name": "retail",
   "state": "loaded",
   "by": "urn:li:corpuser:alice",
   "updated_ms": 1793840800000,
   "namespace": "samples",
   "tables": [
    "customers",
    "orders",
    "products"
   ]
  },
  {
   "name": "retail",
   "state": "not loaded",
   "by": null,
   "updated_ms": null,
   "namespace": "samples",
   "tables": [
    "customers",
    "orders",
    "products"
   ]
  },
  {
   "name": "retail",
   "state": "removed",
   "by": "urn:li:corpuser:tom",
   "updated_ms": 1794013600000,
   "namespace": "samples",
   "tables": [
    "customers",
    "orders",
    "products"
   ]
  }
 ],
 "/v1/derived": [
  {
   "id": "4a2c9e0f7b1d4e85a3c6b9d0e2f17a54",
   "name": "daily-store-revenue",
   "sql": "SELECT store_id, date_trunc('day', ordered_at) AS day, sum(amount) AS revenue FROM lakehouse.sales.orders GROUP BY 1, 2",
   "target": "sales.daily_store_revenue",
   "mode": "merge",
   "watermark": "day",
   "key": [
    "store_id",
    "day"
   ],
   "every_minutes": 60,
   "on_change": true,
   "upstreams": [
    "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
   ],
   "owner": "urn:li:corpuser:alice",
   "enabled": true,
   "last_run_ms": 1794098200000,
   "created_by": "urn:li:corpuser:alice",
   "created_ms": 1791940000000,
   "kind": "table",
   "fresh": true,
   "changed": []
  },
  {
   "id": "9e1b7c3d5a0f42e6b8d4c2a7e9f01b36",
   "name": "customer-ltv",
   "sql": "SELECT c.customer_id, sum(o.amount) AS lifetime_value FROM lakehouse.crm.customers c JOIN lakehouse.sales.orders o ON o.customer_id = c.customer_id GROUP BY 1",
   "target": "analytics.customer_ltv",
   "mode": "full",
   "watermark": null,
   "key": [],
   "every_minutes": null,
   "on_change": true,
   "upstreams": [
    "urn:li:dataset:(urn:li:dataPlatform:iceberg,crm.customers,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
   ],
   "owner": "urn:li:corpuser:mei",
   "enabled": true,
   "last_run_ms": 1794078400000,
   "created_by": "urn:li:corpuser:mei",
   "created_ms": 1788052000000,
   "kind": "materialized_view",
   "fresh": false,
   "changed": [
    "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
   ]
  },
  {
   "id": "c0d8e2a4f6b14c79a1e3d5b7c9f02a68",
   "name": "returns-append",
   "sql": "SELECT * FROM lakehouse.sales.returns",
   "target": "analytics.returns_history",
   "mode": "append",
   "watermark": "returned_at",
   "key": [],
   "every_minutes": 15,
   "on_change": false,
   "upstreams": [
    "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.returns,PROD)"
   ],
   "owner": "urn:li:corpuser:tom",
   "enabled": false,
   "last_run_ms": null,
   "created_by": "urn:li:corpuser:tom",
   "created_ms": 1794013600000,
   "kind": "table",
   "fresh": null,
   "changed": []
  }
 ],
 "/v1/vectors": [
  {
   "id": "1f3b5d7e9a0c42e4b6d8f0a2c4e6b8d0",
   "name": "product-descriptions",
   "source": "catalog.products",
   "key_column": "sku",
   "text_column": "description",
   "chunk_chars": 1500,
   "every_minutes": 60,
   "enabled": true,
   "created_by": "urn:li:corpuser:alice",
   "created_ms": 1792544800000,
   "refreshed_ms": 1794097900000,
   "source_snapshot": 4410298375510293847,
   "chunks": 3842,
   "error": null,
   "ann_lists": null,
   "ann_probes": null,
   "ann_snapshot": null,
   "key": "sku",
   "text": "description"
  },
  {
   "id": "8c0e2a4b6d8f41a3c5e7b9d1f3a5c7e9",
   "name": "support-tickets",
   "source": "support.tickets",
   "key_column": "ticket_id",
   "text_column": "body",
   "chunk_chars": 1000,
   "every_minutes": 15,
   "enabled": true,
   "created_by": "urn:li:corpuser:tom",
   "created_ms": 1793495200000,
   "refreshed_ms": 1794099400000,
   "source_snapshot": 1928374650192837465,
   "chunks": 51207,
   "error": null,
   "ann_lists": 256,
   "ann_probes": 8,
   "ann_snapshot": 6102938475610293847,
   "key": "ticket_id",
   "text": "body"
  },
  {
   "id": "e5a7c9b1d3f540e2a4c6e8b0d2f4a6c8",
   "name": "supplier-docs",
   "source": "procurement.supplier_documents",
   "key_column": "key",
   "text_column": "text",
   "chunk_chars": 1500,
   "every_minutes": null,
   "enabled": false,
   "created_by": "urn:li:corpuser:raj",
   "created_ms": 1794013600000,
   "refreshed_ms": null,
   "source_snapshot": null,
   "chunks": null,
   "error": "the embedding model is not configured (CAIRN_EMBEDDINGS)",
   "ann_lists": null,
   "ann_probes": null,
   "ann_snapshot": null,
   "key": "key",
   "text": "text"
  }
 ],
 "/v1/volumes": [
  {
   "name": "store-photos",
   "owner": "urn:li:corpuser:alice",
   "description": "Shelf photos from store audits, for the planogram model.",
   "bucket": "cairn-vol-store-photos",
   "created_ms": 1791076000000,
   "access": "manage",
   "path": "s3a://cairn-vol-store-photos/",
   "grants": [
    {
     "volume_id": "7a1c3e5f9b2d4f60a8c0e2b4d6f8a1c3",
     "principal": "urn:li:corpuser:raj",
     "access": "read",
     "granted_by": "urn:li:corpuser:alice",
     "created_ms": 1791508000000
    },
    {
     "volume_id": "7a1c3e5f9b2d4f60a8c0e2b4d6f8a1c3",
     "principal": "urn:li:corpGroup:data-science",
     "access": "write",
     "granted_by": "urn:li:corpuser:alice",
     "created_ms": 1791594400000
    }
   ]
  },
  {
   "name": "ml-features-scratch",
   "owner": "urn:li:corpuser:mei",
   "description": "Scratch space for feature engineering experiments.",
   "bucket": "cairn-vol-ml-features-scratch",
   "created_ms": 1793408800000,
   "access": "write",
   "path": "s3a://cairn-vol-ml-features-scratch/"
  },
  {
   "name": "finance-exports",
   "owner": "urn:li:corpuser:tom",
   "description": "Monthly ledger extracts for the auditors.",
   "bucket": "cairn-vol-finance-exports",
   "created_ms": 1781140000000,
   "access": "read",
   "path": "s3a://cairn-vol-finance-exports/"
  }
 ],
 "/storage/v1/buckets": [
  {
   "name": "northwind-lakehouse",
   "created": "2025-06-02T09:14:22.418Z",
   "versioning": "Enabled",
   "usage": {
    "bytes": 3843921455104,
    "objects": 1928114
   },
   "quota": {
    "max_bytes": 5497558138880
   },
   "object_lock": false,
   "encryption_default": true,
   "lifecycle": true,
   "policy": false,
   "disk_type": "ssd",
   "ttl_seconds": null
  },
  {
   "name": "northwind-landing",
   "created": "2025-06-02T09:15:03.007Z",
   "versioning": "Off",
   "usage": {
    "bytes": 128849018880,
    "objects": 412306
   },
   "quota": null,
   "object_lock": false,
   "encryption_default": true,
   "lifecycle": false,
   "policy": true,
   "disk_type": null,
   "ttl_seconds": 2592000
  },
  {
   "name": "northwind-audit-archive",
   "created": "2025-09-18T16:40:51.230Z",
   "versioning": "Enabled",
   "usage": {
    "bytes": 21474836480,
    "objects": 88120
   },
   "quota": {
    "max_bytes": 107374182400,
    "max_objects": 1000000
   },
   "object_lock": true,
   "encryption_default": true,
   "lifecycle": true,
   "policy": false,
   "disk_type": "hdd",
   "ttl_seconds": null
  }
 ],
 "/maintenance/policies": [
  {
   "scope": "*",
   "optimize": true,
   "optimize_threshold": "128MB",
   "expire_days": 14,
   "orphan_days": 7,
   "every_hours": 24,
   "cluster": null,
   "cluster_depth": 4.0,
   "lookup": null,
   "enabled": true,
   "updated_by": "urn:li:corpuser:alice",
   "updated_ms": 1786324000000
  },
  {
   "scope": "sales",
   "optimize": true,
   "optimize_threshold": "256MB",
   "expire_days": 7,
   "orphan_days": 7,
   "every_hours": 6,
   "cluster": "auto",
   "cluster_depth": 4.0,
   "lookup": null,
   "enabled": true,
   "updated_by": "urn:li:corpuser:raj",
   "updated_ms": 1793063200000
  },
  {
   "scope": "inventory.stock_levels",
   "optimize": true,
   "optimize_threshold": "128MB",
   "expire_days": 3,
   "orphan_days": null,
   "every_hours": 12,
   "cluster": [
    "store_id",
    "sku"
   ],
   "cluster_depth": 2.5,
   "lookup": [
    "sku"
   ],
   "enabled": false,
   "updated_by": "urn:li:corpuser:mei",
   "updated_ms": 1794013600000
  }
 ],
 "/maintenance/runs": [
  {
   "id": "c1a2b3d4e5f60718293a4b5c6d7e8f90",
   "table_name": "sales.orders",
   "operation": "optimize",
   "status": "ok",
   "started_ms": 1794097600000,
   "finished_ms": 1794097720000,
   "detail": ""
  },
  {
   "id": "d2b3c4e5f6071829304a5b6c7d8e9f01",
   "table_name": "sales.orders",
   "operation": "expire_snapshots",
   "status": "ok",
   "started_ms": 1794097720000,
   "finished_ms": 1794097780000,
   "detail": ""
  },
  {
   "id": "e3c4d5f60718293a4b5c6d7e8f901a2b",
   "table_name": "inventory.stock_levels",
   "operation": "remove_orphan_files",
   "status": "error",
   "started_ms": 1794089200000,
   "finished_ms": 1794089205000,
   "detail": "Retention specified (3.00d) is shorter than the minimum retention configured in the system (7.00d)"
  }
 ],
 "/v1/procedures": [
  {
   "id": "3e5a7c9e1b3d45f7a9c1e3b5d7f9a1c3",
   "name": "close_store_day",
   "owner": "urn:li:corpuser:raj",
   "connection": "lakehouse",
   "description": "Marks a store's trading day closed and rolls up its takings.",
   "params": [
    {
     "name": "store_id",
     "type": "integer"
    },
    {
     "name": "day",
     "type": "date"
    }
   ],
   "statements": [
    "DELETE FROM lakehouse.sales.day_totals WHERE store_id = :store_id AND day = :day",
    "INSERT INTO lakehouse.sales.day_totals SELECT store_id, CAST(ordered_at AS date), sum(amount) FROM lakehouse.sales.orders WHERE store_id = :store_id AND CAST(ordered_at AS date) = :day GROUP BY 1, 2"
   ],
   "updated_ms": 1793754400000
  },
  {
   "id": "7a9c1e3a5c7e49b1d3f5a7c9e1b3d5f7",
   "name": "top_products",
   "owner": "urn:li:corpuser:alice",
   "connection": "lakehouse",
   "description": "Best sellers in a category over the last N days.",
   "params": [
    {
     "name": "category",
     "type": "varchar"
    },
    {
     "name": "days",
     "type": "integer"
    }
   ],
   "statements": [
    "SELECT p.sku, p.name, sum(o.quantity) AS units FROM lakehouse.sales.orders o JOIN lakehouse.catalog.products p ON p.sku = o.sku WHERE p.category = :category AND o.ordered_at > current_date - INTERVAL '1' DAY * :days GROUP BY 1, 2 ORDER BY units DESC LIMIT 20"
   ],
   "updated_ms": 1792372000000
  },
  {
   "id": "c1e3a5c7e9b14d3f5a7c9e1b3d5f7a9c",
   "name": "flag_fraud_review",
   "owner": "urn:li:corpuser:tom",
   "connection": "lakehouse",
   "description": "Flags an order for manual fraud review.",
   "params": [
    {
     "name": "order_id",
     "type": "varchar"
    },
    {
     "name": "flag",
     "type": "boolean"
    }
   ],
   "statements": [
    "UPDATE lakehouse.sales.orders SET fraud_review = :flag WHERE order_id = :order_id"
   ],
   "updated_ms": 1794089200000
  }
 ],
 "/v1/warehouses": [
  {
   "name": "analytics",
   "port": 8443,
   "state": "running",
   "starts": 41,
   "stops": 40,
   "inflight": 3,
   "idle_minutes": 15,
   "idle_seconds": 0,
   "queries_running": 3,
   "queries_queued": 1,
   "workers": 2
  },
  {
   "name": "finance",
   "port": 8444,
   "state": "stopped",
   "starts": 12,
   "stops": 12,
   "inflight": 0,
   "idle_minutes": 10,
   "idle_seconds": null,
   "queries_running": 0,
   "queries_queued": 0
  },
  {
   "name": "data-science",
   "port": 8445,
   "state": "unreachable",
   "error": "All connection attempts failed"
  }
 ],
 "/v1/databases": {
  "databases": [
   {
    "name": "loyalty_app",
    "urn": "urn:li:pgDatabase:loyalty_app",
    "created_by": "urn:li:corpuser:raj",
    "branch_of": null,
    "created_ms": 1788916000000,
    "host": "pg.northwind.internal",
    "port": 5432,
    "database": "loyalty_app"
   },
   {
    "name": "loyalty_app_test",
    "urn": "urn:li:pgDatabase:loyalty_app_test",
    "created_by": "urn:li:corpuser:raj",
    "branch_of": "loyalty_app",
    "created_ms": 1793927200000,
    "host": "pg.northwind.internal",
    "port": 5432,
    "database": "loyalty_app_test"
   },
   {
    "name": "store_scheduler",
    "urn": "urn:li:pgDatabase:store_scheduler",
    "created_by": "urn:li:corpuser:tom",
    "branch_of": null,
    "created_ms": 1784596000000,
    "host": "pg.northwind.internal",
    "port": 5432,
    "database": "store_scheduler"
   }
  ]
 },
 "/v1/jobs": [
  {
   "id": "5b7d9f1a3c5e47a9b1d3f5a7c9e1b3d5",
   "name": "weekly-demand-forecast",
   "owner": "urn:li:corpuser:mei",
   "kind": "notebook",
   "params": {
    "horizon_weeks": 8
   },
   "cron": "0 2 * * 1",
   "timeout_minutes": 90,
   "recipients": [
    "urn:li:corpuser:alice"
   ],
   "notify": "failure",
   "enabled": true,
   "next_run_ms": 1794272800000,
   "created_ms": 1790644000000,
   "updated_ms": 1793840800000,
   "source_bytes": 48211
  },
  {
   "id": "a2c4e6a8b0d24f6a8c0e2a4c6e8a0c2e",
   "name": "refresh-price-elasticity",
   "owner": "urn:li:corpuser:alice",
   "kind": "python",
   "params": {},
   "cron": "30 4 * * *",
   "timeout_minutes": 30,
   "recipients": [],
   "notify": "always",
   "enabled": true,
   "next_run_ms": 1794114400000,
   "created_ms": 1793063200000,
   "updated_ms": 1793063200000,
   "source_bytes": 3120
  },
  {
   "id": "f0e1d2c3b4a5469788796a5b4c3d2e1f",
   "name": "one-off-backfill",
   "owner": "urn:li:corpuser:tom",
   "kind": "python",
   "params": {
    "start": "2026-01-01"
   },
   "cron": null,
   "timeout_minutes": 120,
   "recipients": [
    "urn:li:corpuser:raj"
   ],
   "notify": "failure",
   "enabled": false,
   "next_run_ms": null,
   "created_ms": 1793668000000,
   "updated_ms": 1793668000000,
   "source_bytes": 1874
  }
 ],
 "/v1/apps": [
  {
   "id": "0a2c4e6a8c0e42a4c6e8a0c2e4a6c8e0",
   "name": "store-dashboard",
   "title": "Store performance",
   "url": null,
   "idle_minutes": 30,
   "runtime": "kubernetes",
   "port": 8501,
   "commit": "9f2c1ab7e4d03c58a1b6e9f0d2c4a7b3e5f81d0c",
   "deployed_ms": 1793927200000,
   "created_by": "urn:li:corpuser:alice",
   "created_ms": 1790212000000,
   "updated_ms": 1793927200000,
   "running": null
  },
  {
   "id": "4e6a8c0e2a4c46e8a0c2e4a6c8e0a2c4",
   "name": "markdown-planner",
   "title": "Markdown planner",
   "url": null,
   "idle_minutes": 20,
   "runtime": "local",
   "port": null,
   "commit": null,
   "deployed_ms": null,
   "created_by": "urn:li:corpuser:mei",
   "created_ms": 1793322400000,
   "updated_ms": 1793322400000,
   "running": true
  },
  {
   "id": "8c0e2a4c6e8a40c2e4a6c8e0a2c4e6a8",
   "name": "supplier-portal",
   "title": "Supplier scorecards",
   "url": "http://supplier-portal.apps.svc:8080",
   "idle_minutes": 30,
   "runtime": "local",
   "port": null,
   "commit": null,
   "deployed_ms": null,
   "created_by": "urn:li:corpuser:raj",
   "created_ms": 1787188000000,
   "updated_ms": 1791508000000,
   "running": null
  }
 ],
 "/v1/spark/history/api/v1/applications": [
  {
   "id": "spark-7f3c1e0a9b2d4c8e",
   "name": "nightly-orders-compaction",
   "attempts": [
    {
     "startTime": "2026-11-07T17:06:40.000GMT",
     "endTime": "2026-11-07T17:27:40.000GMT",
     "lastUpdated": "2026-11-07T17:27:40.000GMT",
     "duration": 1260000,
     "sparkUser": "raj",
     "completed": true,
     "appSparkVersion": "3.5.3",
     "startTimeEpoch": 1794071200000,
     "endTimeEpoch": 1794072460000,
     "lastUpdatedEpoch": 1794072460000
    }
   ]
  },
  {
   "id": "spark-2a8d6b4f1c0e4a97",
   "name": "demand-forecast-features",
   "attempts": [
    {
     "startTime": "2026-11-07T22:06:40.000GMT",
     "endTime": "2026-11-07T22:54:00.000GMT",
     "lastUpdated": "2026-11-07T22:54:00.000GMT",
     "duration": 2840000,
     "sparkUser": "mei",
     "completed": true,
     "appSparkVersion": "3.5.3",
     "startTimeEpoch": 1794089200000,
     "endTimeEpoch": 1794092040000,
     "lastUpdatedEpoch": 1794092040000
    }
   ]
  },
  {
   "id": "spark-9e0b3d5a7c1f4e26",
   "name": "clickstream-sessionize",
   "attempts": [
    {
     "startTime": "2026-11-08T00:51:40.000GMT",
     "endTime": "1969-12-31T23:59:59.999GMT",
     "lastUpdated": "2026-11-08T01:01:40.000GMT",
     "duration": 600000,
     "sparkUser": "tom",
     "completed": false,
     "appSparkVersion": "3.5.3",
     "startTimeEpoch": 1794099100000,
     "endTimeEpoch": -1,
     "lastUpdatedEpoch": 1794099700000
    }
   ]
  }
 ],
 "/v1/agent-apps": [
  {
   "name": "store-ops-helper",
   "agent_id": "agt_3f9a1c7e2b40",
   "urn": "urn:li:cairnAgent:store-ops-helper",
   "owner": "urn:li:corpuser:alice",
   "description": "Answers store managers' questions about weekly sales and stock levels.",
   "template": "sql-analyst",
   "model": "claude-sonnet",
   "scopes": [
    "model",
    "query_data",
    "read"
   ],
   "version": 4,
   "status": "running",
   "error": null,
   "mlflow_experiment": "112",
   "updated_ms": 1794092800000
  },
  {
   "name": "returns-triage",
   "agent_id": "agt_8c21d04a9e17",
   "urn": "urn:li:cairnAgent:returns-triage",
   "owner": "urn:li:corpuser:raj",
   "description": "Classifies return reasons and drafts a summary for the returns team.",
   "template": null,
   "model": "gpt-4o-mini",
   "scopes": [
    "model",
    "read"
   ],
   "version": 2,
   "status": "failed",
   "error": "the deploy needs the package 'rapidfuzz', which is not installed",
   "mlflow_experiment": "118",
   "updated_ms": 1794006400000
  },
  {
   "name": "supplier-briefing",
   "agent_id": "agt_51be6f03c8d2",
   "urn": "urn:li:cairnAgent:supplier-briefing",
   "owner": "urn:li:corpuser:mei",
   "description": "Prepares a briefing on a supplier's deliveries, delays and open purchase orders.",
   "template": "research",
   "model": "claude-sonnet",
   "scopes": [
    "model",
    "propose",
    "query_data",
    "read"
   ],
   "version": 1,
   "status": "running",
   "error": null,
   "mlflow_experiment": null,
   "updated_ms": 1793668000000
  }
 ],
 "/v1/custom-agents": [
  {
   "name": "catalog-gardener",
   "owner": "urn:li:corpuser:alice",
   "team": "urn:li:corpGroup:data-platform",
   "description": "Finds undocumented tables and suggests descriptions and owners.",
   "tools": [
    "get_asset",
    "search_assets",
    "suggest_description",
    "suggest_owner"
   ],
   "scopes": [
    "read",
    "propose"
   ],
   "ai_system": null,
   "max_steps": 8,
   "deployed": true,
   "version": 3,
   "deployed_version": 3,
   "agent_id": "agt_a4c91e0f5d22",
   "created_ms": 1792372000000,
   "updated_ms": 1793840800000,
   "instructions": "Look for tables in the sales and inventory domains with no description or owner, and suggest both."
  },
  {
   "name": "weekly-sales-explainer",
   "owner": "urn:li:corpuser:raj",
   "team": "urn:li:corpGroup:merchandising",
   "description": "Explains week-over-week changes in sales by region using governed metrics.",
   "tools": [
    "ask_data",
    "list_metrics",
    "query_metric"
   ],
   "scopes": [
    "read",
    "query_data"
   ],
   "ai_system": "ais_7d1f20b3",
   "max_steps": 6,
   "deployed": false,
   "version": 5,
   "deployed_version": 4,
   "agent_id": "agt_0b7e3d9c1a58",
   "created_ms": 1790644000000,
   "updated_ms": 1794078400000
  },
  {
   "name": "lineage-detective",
   "owner": "urn:li:corpuser:mei",
   "team": null,
   "description": "Traces a broken dashboard back to the upstream table that changed.",
   "tools": [
    "explain_lineage",
    "get_lineage",
    "get_quality_status",
    "impact_analysis"
   ],
   "scopes": [
    "read"
   ],
   "ai_system": null,
   "max_steps": 10,
   "deployed": true,
   "version": 1,
   "deployed_version": 1,
   "agent_id": "agt_e6f2a8b41c03",
   "created_ms": 1793322400000,
   "updated_ms": 1793322400000
  }
 ],
 "/ai/scheduled-tasks": [
  {
   "id": "3a7f0c92e15b4d68b0c4e9a21f7d5b30",
   "owner": "urn:li:corpuser:alice",
   "name": "Weekly revenue by region",
   "prompt": "Every Monday, summarise last week's revenue by region and flag anything unusual.",
   "cron": "0 7 * * 1",
   "max_steps": 4,
   "send": {
    "slack": "#sales-leads"
   },
   "enabled": true,
   "failures": 0,
   "next_run_ms": 1794272800000,
   "last_run_ms": 1793668000000,
   "last_status": "done",
   "created_ms": 1788916000000
  },
  {
   "id": "c51e8d07a24f4b93a6e2d0f7b18c9e45",
   "owner": "urn:li:corpuser:alice",
   "name": "Stock-outs digest",
   "prompt": "Each morning list stores with stock-outs on top-50 products yesterday.",
   "cron": "30 6 * * *",
   "max_steps": 3,
   "send": {
    "email": [
     "store-ops@northwind.example"
    ]
   },
   "enabled": true,
   "failures": 1,
   "next_run_ms": 1794172000000,
   "last_run_ms": 1794085600000,
   "last_status": "failed",
   "created_ms": 1793063200000
  },
  {
   "id": "e0b4a6f3d9c2417e8a51c7d0b36f2e94",
   "owner": "urn:li:corpuser:alice",
   "name": "Monthly returns review",
   "prompt": "On the first of the month, review last month's returns by reason and category.",
   "cron": "0 8 1 * *",
   "max_steps": 6,
   "send": null,
   "enabled": false,
   "failures": 3,
   "next_run_ms": 1796087200000,
   "last_run_ms": 1793495200000,
   "last_status": "failed",
   "created_ms": 1783732000000
  }
 ],
 "/v1/agent-traces/insights": {
  "days": 7,
  "since_ms": 1793490400000,
  "until_ms": 1794095200000,
  "baseline_days": 21,
  "settings": {
   "loop_threshold": 4,
   "slow_call_ms": 10000,
   "outlier_factor": 2.0,
   "min_calls": 10,
   "error_rate_warning": 0.2,
   "error_rate_critical": 0.5
  },
  "truncated": false,
  "agents": [
   {
    "agent": "agt_5f20c1a9e7b4",
    "name": "Revenue analyst",
    "kind": "built",
    "owner": "urn:li:corpuser:alice",
    "enabled": true,
    "runs": 58,
    "tool_calls": 402,
    "errors": 9,
    "refused": 2,
    "error_rate": 0.0224,
    "refusal_rate": 0.005,
    "flagged": 0,
    "people": 4,
    "avg_ms": 412.5,
    "p95_ms": 2210,
    "slow_calls": 3,
    "loops": 1,
    "model": {
     "calls": 174,
     "input_tokens": 2436000,
     "output_tokens": 179800,
     "cost_usd": 21.84
    },
    "cost_per_run_usd": 0.376552,
    "last_ms": 1794091600000,
    "baseline": {
     "days": 21,
     "tool_calls": 930,
     "calls_per_day": 44.29,
     "error_rate": 0.018,
     "p95_ms": 1980,
     "cost_per_day_usd": 1.2,
     "calls_per_run": 6.8
    }
   },
   {
    "agent": "agt_91c3d7e2a0f5",
    "name": "Support triage",
    "kind": "app",
    "owner": "urn:li:corpuser:bob",
    "enabled": true,
    "runs": 40,
    "tool_calls": 128,
    "errors": 31,
    "refused": 2,
    "error_rate": 0.2422,
    "refusal_rate": 0.0156,
    "flagged": 0,
    "people": 4,
    "avg_ms": 412.5,
    "p95_ms": 860,
    "slow_calls": 0,
    "loops": 0,
    "model": {
     "calls": 120,
     "input_tokens": 1680000,
     "output_tokens": 124000,
     "cost_usd": 4.4
    },
    "cost_per_run_usd": 0.11,
    "last_ms": 1794080800000,
    "baseline": {
     "days": 21,
     "tool_calls": 402,
     "calls_per_day": 19.14,
     "error_rate": 0.21,
     "p95_ms": 900,
     "cost_per_day_usd": 0.61,
     "calls_per_run": 3.2
    }
   }
  ],
  "findings": [
   {
    "id": "f_3c91e0a7b52d",
    "agent": "agt_5f20c1a9e7b4",
    "agent_name": "Revenue analyst",
    "kind": "loop",
    "severity": "critical",
    "title": "get_quality_status repeated 9 times in one run",
    "detail": "The agent called get_quality_status with the same arguments 9 times in run 7e1c0b9a2f4d4c83a5d6e0f19b2c7a48 (threshold 4): it may be stuck retrying.",
    "value": 9,
    "baseline": 4,
    "run": "7e1c0b9a2f4d4c83a5d6e0f19b2c7a48",
    "tool": "get_quality_status"
   },
   {
    "id": "f_a0d47c2e19b8",
    "agent": "agt_91c3d7e2a0f5",
    "agent_name": "Support triage",
    "kind": "error_rate",
    "severity": "warning",
    "title": "24% of tool calls failed",
    "detail": "31 of 128 tool calls ended in an error.",
    "value": 0.2422,
    "baseline": 0.2
   },
   {
    "id": "f_6b2e93d0c4a1",
    "agent": "agt_5f20c1a9e7b4",
    "agent_name": "Revenue analyst",
    "kind": "cost_outlier",
    "severity": "warning",
    "title": "Model cost per day 2.6x its baseline",
    "detail": "Model cost per day was 3.12 USD against a baseline of 1.2 USD (over the 21 days before).",
    "value": 3.12,
    "baseline": 1.2
   }
  ],
  "totals": {
   "agents": 2,
   "runs": 98,
   "tool_calls": 530,
   "errors": 40,
   "cost_usd": 26.24,
   "critical": 1,
   "warning": 2
  }
 },
 "/v1/agent-intents/denials": [
  {
   "id": "7c0e4b2a-91d3-4f6e-a8b5-2d1f0c9e3a74",
   "ts": "2026-11-07T14:21:08.412000+00:00",
   "agent": "agt_91c3d7e2a0f5",
   "actor": "urn:li:corpuser:bob",
   "category": "export",
   "reason": "export is outside this run's declared intent (triage open tickets: read, query, model)",
   "route": "/context/definitions/export",
   "tool": null,
   "dataset": null,
   "run": "run_3f9a1c07d2e84b56",
   "purpose": "triage open tickets"
  },
  {
   "id": "b5d21f80-3e6c-4a97-9c0b-71e4a2f8d613",
   "ts": "2026-11-07T09:02:51.077000+00:00",
   "agent": "agt_5f20c1a9e7b4",
   "actor": "urn:li:corpuser:alice",
   "category": "query",
   "reason": "hr.public.salaries is outside this run's declared intent (weekly revenue summary)",
   "route": "sql",
   "tool": null,
   "dataset": "urn:li:dataset:(urn:li:dataPlatform:postgres,hr.public.salaries,PROD)",
   "run": "8e2d40b7c1f94a3e9b6d05c7a1f2e8d4",
   "purpose": "weekly revenue summary"
  }
 ],
 "/ai/outbox": [
  {
   "id": "9d2b7e41c08a4f5f93e6a1b0c7d42e18",
   "owner": "urn:li:corpuser:alice",
   "task_id": "3a7f0c92e15b4d68b0c4e9a21f7d5b30",
   "kind": "slack",
   "target": "#sales-leads",
   "subject": "Weekly revenue by region",
   "artifact_id": "art_5e0c9a17b3d24f68",
   "artifact_version": 1,
   "status": "pending",
   "created_ms": 1794089200000,
   "decided_ms": null,
   "error": null
  },
  {
   "id": "4f81c3a0e92d4b7c8e5a6d1f0b29c73e",
   "owner": "urn:li:corpuser:alice",
   "task_id": "c51e8d07a24f4b93a6e2d0f7b18c9e45",
   "kind": "email",
   "target": [
    "store-ops@northwind.example"
   ],
   "subject": "Stock-outs digest",
   "artifact_id": "art_b19e2d70c4a83f51",
   "artifact_version": 1,
   "status": "sent",
   "created_ms": 1793999200000,
   "decided_ms": 1794002800000,
   "error": null
  },
  {
   "id": "a06e5d29b17c4e3f8d2c90a4b6e1f357",
   "owner": "urn:li:corpuser:alice",
   "task_id": null,
   "kind": "email",
   "target": [
    "finance@northwind.example"
   ],
   "subject": "Q3 margin by category",
   "artifact_id": "art_27c4f08e91b5d3a6",
   "artifact_version": 2,
   "status": "refused",
   "created_ms": 1793754400000,
   "decided_ms": 1793755000000,
   "error": "you may not export urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.finance.margins,PROD)"
  }
 ],
 "/v1/pipeline-agent/tasks": [
  {
   "id": "7c3e9a0d1b5f4e82a6c4d0e9f1b27a35",
   "kind": "build",
   "target": "analytics.daily_revenue_by_region",
   "status": "ready",
   "created_ms": 1794098200000
  },
  {
   "id": "1e8b4d2c7a094f6e83d5c1a0b9f72e64",
   "kind": "fix",
   "target": "analytics.store_inventory_snapshot",
   "status": "shipped",
   "created_ms": 1793927200000
  },
  {
   "id": "d4a0f7e2c93b41d58e6a2b1c07f9e3d8",
   "kind": "build",
   "target": "analytics.customer_ltv",
   "status": "failed",
   "created_ms": 1793581600000
  }
 ],
 "/ai/steward/proposals": [
  {
   "id": "sp_4e1a9c07b3d25f",
   "urn": "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.sales.orders,PROD)",
   "kind": "owner",
   "field": "",
   "value": "urn:li:corpuser:raj",
   "evidence": [
    "ran 412 of the 980 recent queries",
    "owns 2 upstream table(s)"
   ],
   "confidence": 0.82,
   "state": "PROPOSED",
   "priority": 0.94,
   "decided_by": null,
   "batch_id": null,
   "name": "orders"
  },
  {
   "id": "sp_b07d3e9f12a64c",
   "urn": "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.crm.loyalty_members,PROD)",
   "kind": "term",
   "field": "email",
   "value": "urn:li:glossaryTerm:Classification.PII.EMAIL",
   "evidence": [
    "column name matches email",
    "sample values look like email addresses"
   ],
   "confidence": 0.95,
   "state": "PROPOSED",
   "priority": 0.71,
   "decided_by": null,
   "batch_id": null,
   "name": "loyalty_members"
  },
  {
   "id": "sp_9c25f0a8e4b7d1",
   "urn": "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.inventory.stock_levels,PROD)",
   "kind": "description",
   "field": "",
   "value": {
    "description": "Daily on-hand units per store and product, taken at store close.",
    "columns": [
     {
      "field_path": "on_hand_units",
      "description": "Units on the shelf and in the back room at close.",
      "suggested_terms": []
     }
    ],
    "model": "claude-sonnet-4-5"
   },
   "evidence": [
    "drafted from the schema, lineage and neighbouring documentation",
    "unknown: whether in-transit units are included"
   ],
   "confidence": 0.64,
   "state": "PROPOSED",
   "priority": 0.58,
   "decided_by": null,
   "batch_id": null,
   "name": "stock_levels"
  }
 ],
 "/admin/agents": [
  {
   "id": "agt_5c0e7a12f93b",
   "name": "Nightly catalog sync",
   "kind": "key",
   "owner": "urn:li:corpuser:tom",
   "scopes": [
    "read",
    "write"
   ],
   "autonomy": "suggest",
   "auto_accept_at": 0.9,
   "per_minute": 120,
   "daily_requests": 20000,
   "redirect_uris": [],
   "ai_system": null,
   "enabled": true,
   "created_by": "urn:li:corpuser:alice",
   "created_ms": 1788916000000,
   "last_used_ms": 1794097600000,
   "description": "Pushes tag changes from the merchandising tool.",
   "requests_today": 1834
  },
  {
   "id": "agt_9a3b61d0e7c4",
   "name": "Claude Code",
   "kind": "oauth",
   "owner": null,
   "scopes": [
    "read",
    "query_data",
    "propose"
   ],
   "autonomy": "suggest",
   "auto_accept_at": 0.9,
   "per_minute": 120,
   "daily_requests": 20000,
   "redirect_uris": [
    "http://localhost:33418/callback"
   ],
   "ai_system": "ais_7d1f20b3",
   "enabled": true,
   "created_by": "urn:li:corpuser:raj",
   "created_ms": 1792890400000,
   "last_used_ms": 1794099700000,
   "software_id": "claude-code",
   "requests_today": 412
  },
  {
   "id": "agt_a4c91e0f5d22",
   "name": "catalog-gardener",
   "kind": "built",
   "owner": "urn:li:corpuser:alice",
   "scopes": [
    "read",
    "propose"
   ],
   "autonomy": "auto",
   "auto_accept_at": 0.95,
   "per_minute": 60,
   "daily_requests": 5000,
   "redirect_uris": [],
   "ai_system": null,
   "enabled": false,
   "created_by": "urn:li:corpuser:alice",
   "created_ms": 1792372000000,
   "last_used_ms": 1793927200000,
   "description": "Finds undocumented tables and suggests descriptions and owners.",
   "requests_today": 0
  }
 ],
 "/agents": [
  {
   "agent_id": "agt_1f4e9b07c2d35a68",
   "pool": "store-dc-east",
   "hostname": "nw-ingest-01.dc-east.northwind.local",
   "version": "1.14.2",
   "status": "ONLINE",
   "last_heartbeat": "2026-11-08T09:42:11.204000+00:00",
   "enrolled_at": "2026-08-19T13:05:40+00:00",
   "running_tasks": 1,
   "last_ip": "10.20.4.17"
  },
  {
   "agent_id": "agt_7b2c0d91e8a4f356",
   "pool": "store-dc-east",
   "hostname": "nw-ingest-02.dc-east.northwind.local",
   "version": "1.14.2",
   "status": "OFFLINE",
   "last_heartbeat": "2026-11-07T22:10:03.550000+00:00",
   "enrolled_at": "2026-08-19T13:09:12+00:00",
   "running_tasks": 0,
   "last_ip": "10.20.4.18"
  },
  {
   "agent_id": "agt_c93a5e10b74d2f81",
   "pool": "erp-onprem",
   "hostname": "erp-gw.northwind.local",
   "version": "1.13.0",
   "status": "REVOKED",
   "last_heartbeat": "2026-10-02T07:55:48+00:00",
   "enrolled_at": "2026-05-11T10:20:00+00:00",
   "running_tasks": 0,
   "last_ip": "192.168.10.5"
  }
 ],
 "/v1/ml/api/2.0/mlflow/registered-models/search": {
  "registered_models": [
   {
    "name": "demand-forecast",
    "creation_timestamp": 1783732000000,
    "last_updated_timestamp": 1793927200000,
    "user_id": "alice",
    "description": "Weekly unit demand per store and product.",
    "latest_versions": [
     {
      "name": "demand-forecast",
      "version": "7",
      "creation_timestamp": 1793927200000,
      "last_updated_timestamp": 1793927200000,
      "current_stage": "None",
      "source": "mlflow-artifacts:/14/2b6f0e9a1c3d/artifacts/model",
      "run_id": "2b6f0e9a1c3d4e5f8a7b6c5d4e3f2a1b",
      "status": "READY",
      "user_id": "alice"
     }
    ],
    "tags": [
     {
      "key": "team",
      "value": "supply-chain"
     }
    ],
    "aliases": [
     {
      "alias": "champion",
      "version": "6"
     }
    ]
   },
   {
    "name": "churn-propensity",
    "creation_timestamp": 1776820000000,
    "last_updated_timestamp": 1791508000000,
    "user_id": "raj",
    "description": "Probability a loyalty member lapses within 90 days.",
    "latest_versions": [
     {
      "name": "churn-propensity",
      "version": "3",
      "creation_timestamp": 1791508000000,
      "last_updated_timestamp": 1791508000000,
      "current_stage": "None",
      "source": "mlflow-artifacts:/9/7d1e4c0b2a9f/artifacts/model",
      "run_id": "7d1e4c0b2a9f4b3e8c6d5a4f3e2d1c0b",
      "status": "READY",
      "user_id": "raj"
     }
    ],
    "tags": [],
    "aliases": [
     {
      "alias": "champion",
      "version": "3"
     }
    ]
   },
   {
    "name": "basket-recommender",
    "creation_timestamp": 1793236000000,
    "last_updated_timestamp": 1793236000000,
    "user_id": "mei",
    "latest_versions": [
     {
      "name": "basket-recommender",
      "version": "1",
      "creation_timestamp": 1793236000000,
      "last_updated_timestamp": 1793236000000,
      "current_stage": "None",
      "source": "mlflow-artifacts:/21/c3a9e0f17b2d/artifacts/model",
      "run_id": "c3a9e0f17b2d4c8a9e6f5b4a3d2c1e0f",
      "status": "READY",
      "user_id": "mei"
     }
    ],
    "tags": [
     {
      "key": "team",
      "value": "ecommerce"
     }
    ],
    "aliases": []
   }
  ]
 },
 "/v1/ml/deployments": [
  {
   "name": "demand-forecast-prod",
   "owner": "urn:li:corpuser:alice",
   "model": "demand-forecast",
   "version": "6",
   "min_replicas": 1,
   "max_replicas": 4,
   "target_concurrency": 2.0,
   "idle_minutes": 10,
   "replicas": 2,
   "last_request_ms": 1794099985000,
   "running": 2,
   "in_flight": 1
  },
  {
   "name": "churn-scoring",
   "owner": "urn:li:corpuser:raj",
   "model": "churn-propensity",
   "version": "3",
   "min_replicas": 0,
   "max_replicas": 2,
   "target_concurrency": 4.0,
   "idle_minutes": 15,
   "replicas": 0,
   "last_request_ms": 1794089200000,
   "running": 0,
   "in_flight": 0
  },
  {
   "name": "basket-recs-canary",
   "owner": "urn:li:corpuser:mei",
   "model": "basket-recommender",
   "version": "1",
   "min_replicas": 0,
   "max_replicas": 1,
   "target_concurrency": 2.0,
   "idle_minutes": 10,
   "replicas": 1,
   "last_request_ms": 1794099760000,
   "running": 1,
   "in_flight": 0
  }
 ],
 "/v1/ml/feature-tables": [
  {
   "name": "store_weekly_features",
   "urn": "urn:li:mlFeatureTable:(urn:li:dataPlatform:iceberg,store_weekly_features)",
   "table": "features.store_weekly",
   "source": "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.features.store_weekly,PROD)",
   "keys": [
    "store_id",
    "week_start"
   ],
   "features": [
    "units_sold",
    "revenue",
    "footfall",
    "promo_days"
   ],
   "description": "Weekly sales and traffic per store."
  },
  {
   "name": "customer_rfm",
   "urn": "urn:li:mlFeatureTable:(urn:li:dataPlatform:iceberg,customer_rfm)",
   "table": "features.customer_rfm",
   "source": "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.features.customer_rfm,PROD)",
   "keys": [
    "customer_id"
   ],
   "features": [
    "recency_days",
    "frequency_90d",
    "monetary_90d",
    "loyalty_tier"
   ],
   "description": "Recency, frequency and spend per loyalty member."
  },
  {
   "name": "product_attributes",
   "urn": "urn:li:mlFeatureTable:(urn:li:dataPlatform:iceberg,product_attributes)",
   "table": "features.product_attributes",
   "source": "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.features.product_attributes,PROD)",
   "keys": [
    "product_id"
   ],
   "features": [
    "category",
    "price",
    "shelf_life_days",
    "is_private_label"
   ],
   "description": null
  }
 ],
 "/v1/ml/finetunes": {
  "finetunes": [
   {
    "name": "ticket-triage",
    "urn": "urn:li:finetune:ticket-triage",
    "base_model": "distilgpt2",
    "status": "done",
    "error": null,
    "examples": 842,
    "redacted_values": 37,
    "sources": [
     "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.support.tickets,PROD)"
    ],
    "metrics": {
     "examples": 842,
     "epochs": 3,
     "epoch_loss": [
      2.41873,
      1.30552,
      0.98211
     ],
     "trainable_parameters": 147456,
     "seconds": 312.4,
     "lora": true
    },
    "lora": true,
    "learning_rate": 0.0005,
    "epochs": 3,
    "source": {
     "connection": "lakehouse",
     "sql": "SELECT subject, team FROM support.tickets"
    },
    "prompt_template": "Ticket: {prompt}\nTeam:",
    "created_by": "urn:li:corpuser:tom",
    "created_ms": 1793840800000,
    "finished_ms": 1793841160000
   },
   {
    "name": "product-copy",
    "urn": "urn:li:finetune:product-copy",
    "base_model": "Qwen/Qwen2.5-0.5B",
    "status": "running",
    "error": null,
    "examples": 1000,
    "redacted_values": 0,
    "sources": [
     "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.catalog.products,PROD)",
     "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.catalog.product_descriptions,PROD)"
    ],
    "metrics": null,
    "lora": true,
    "learning_rate": 0.0002,
    "epochs": 2,
    "source": {
     "connection": "lakehouse",
     "sql": "SELECT p.name, d.copy FROM catalog.products p JOIN catalog.product_descriptions d ON p.id = d.product_id"
    },
    "prompt_template": "Product: {prompt}\nDescription:",
    "created_by": "urn:li:corpuser:mei",
    "created_ms": 1794097600000,
    "finished_ms": null
   },
   {
    "name": "return-reason",
    "urn": "urn:li:finetune:return-reason",
    "base_model": "distilgpt2",
    "status": "failed",
    "error": "the loss is not finite: lower the learning rate",
    "examples": 214,
    "redacted_values": 12,
    "sources": [
     "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.returns.return_lines,PROD)"
    ],
    "metrics": null,
    "lora": false,
    "learning_rate": 0.01,
    "epochs": 5,
    "source": {
     "connection": "lakehouse",
     "sql": "SELECT comment, reason_code FROM returns.return_lines"
    },
    "prompt_template": "Comment: {prompt}\nReason:",
    "created_by": "urn:li:corpuser:raj",
    "created_ms": 1793408800000,
    "finished_ms": 1793408920000
   }
  ]
 },
 "/governance/access-reviews": [
  {
   "id": "rev_3f9a1c7e5b2d4a60",
   "name": "Q4 2026 finance data access",
   "state": "open",
   "due_ms": 1794877600000
  },
  {
   "id": "rev_8c2e7a1f0d9b3c54",
   "name": "Customer PII recertification",
   "state": "open",
   "due_ms": 1794359200000
  },
  {
   "id": "rev_1b7d4e9a2c6f8e03",
   "name": "Q3 2026 supplier analytics",
   "state": "closed",
   "due_ms": 1790644000000
  }
 ],
 "/governance/residency": {
  "region": "eu",
  "regions": {
   "eu": "s3://northwind-lake-eu/warehouse",
   "us": "s3://northwind-lake-us/warehouse"
  },
  "pins": [
   {
    "namespaces": [
     "crm_eu",
     "hr_*"
    ],
    "region": "eu",
    "allow": [
     "eu"
    ]
   },
   {
    "namespaces": [
     "loyalty_eu"
    ],
    "region": "eu",
    "allow": [
     "eu"
    ]
   },
   {
    "namespaces": [
     "sales_us"
    ],
    "region": "us",
    "allow": [
     "eu",
     "us"
    ]
   }
  ]
 },
 "/governance/posture": {
  "id": "9f3c2a7d1e8b4c6a0d5e7f1a2b3c4d5e",
  "ts_ms": 1794100000000,
  "score": 71,
  "findings": [
   {
    "check": "pii_unmasked",
    "severity": "high",
    "title": "Sensitive columns exist and no policy masks anything",
    "evidence": {
     "datasets": 14,
     "mask_policies": 0
    },
    "fix": "Add a masking policy for the PII classifications."
   },
   {
    "check": "idle_shares",
    "severity": "medium",
    "title": "2 share(s) to outside recipients have not been used for 90 days",
    "evidence": {
     "shares": [
      "share_contoso_returns",
      "share_fabrikam_sales"
     ]
    },
    "fix": "Revoke the shares nobody reads."
   },
   {
    "check": "no_access_review",
    "severity": "medium",
    "title": "Grants exist and no access review closed in the last 180 days",
    "evidence": {
     "grants": 37
    },
    "fix": "Start an access review (POST /governance/access-reviews)."
   },
   {
    "check": "ai_budget",
    "severity": "low",
    "title": "A model is configured and there is no daily AI budget",
    "evidence": {},
    "fix": "Set CAIRN_AI_DAILY_BUDGET_USD."
   }
  ],
  "checks": {
   "trusted_header": "ok",
   "authz_mode": "ok",
   "open_policy": "ok",
   "pii_unmasked": "finding",
   "audit_chain": "ok",
   "long_shares": "ok",
   "idle_shares": "finding",
   "no_access_review": "finding",
   "residency_region": "ok",
   "ai_budget": "finding",
   "secrets_key": "ok"
  }
 },
 "/governance/tokens/domains": {
  "domains": [
   {
    "name": "email",
    "description": "Customer e-mail addresses",
    "created_by": "urn:li:corpuser:alice",
    "created_ms": 1783732000000
   },
   {
    "name": "loyalty_id",
    "description": "Northwind Rewards member numbers",
    "created_by": "urn:li:corpuser:mei",
    "created_ms": 1788570400000
   },
   {
    "name": "iban",
    "description": "Supplier bank accounts",
    "created_by": "urn:li:corpuser:alice",
    "created_ms": 1793063200000
   }
  ]
 },
 "/governance/consent/purposes": [
  {
   "id": "marketing",
   "name": "Marketing",
   "description": "E-mail and ad audience targeting",
   "identifier": "email",
   "mode": "opt_out",
   "members": {
    "users": [
     "urn:li:corpuser:tom"
    ],
    "groups": [
     "urn:li:corpGroup:marketing"
    ],
    "personas": []
   },
   "enabled": true,
   "updated_by": "urn:li:corpuser:alice",
   "updated_ms": 1793668000000,
   "records": {
    "granted": 0,
    "withdrawn": 2314
   },
   "inline_limit": 10000,
   "filter_state": "ok"
  },
  {
   "id": "loyalty-personalisation",
   "name": "Loyalty personalisation",
   "description": "Personalised offers in the Northwind Rewards app",
   "identifier": "customer_id",
   "mode": "opt_in",
   "members": {
    "users": [],
    "groups": [
     "urn:li:corpGroup:loyalty"
    ],
    "personas": [
     "loyalty-analyst"
    ]
   },
   "enabled": true,
   "updated_by": "urn:li:corpuser:mei",
   "updated_ms": 1792285600000,
   "records": {
    "granted": 8412,
    "withdrawn": 97
   },
   "inline_limit": 10000,
   "filter_state": "ok"
  },
  {
   "id": "sms-promotions",
   "name": "SMS promotions",
   "description": "",
   "identifier": "phone",
   "mode": "opt_out",
   "members": {
    "users": [
     "urn:li:corpuser:raj"
    ],
    "groups": [],
    "personas": []
   },
   "enabled": false,
   "updated_by": "urn:li:corpuser:alice",
   "updated_ms": 1794092800000,
   "records": {
    "granted": 0,
    "withdrawn": 12950
   },
   "inline_limit": 10000,
   "filter_state": "over_limit: rows are withheld until it is reduced"
  }
 ],
 "/quality/contracts": [
  {
   "dataset": "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.sales.orders,PROD)",
   "version": "2.1.0",
   "owner": "urn:li:corpuser:raj",
   "status": "MET",
   "parts": [
    {
     "part": "schema",
     "status": "MET",
     "detail": "3 column(s) as promised"
    },
    {
     "part": "freshness",
     "name": "freshness",
     "monitor_id": "contract-9d1f2a-freshness",
     "status": "MET",
     "detail": "last updated 1.4 hours ago"
    }
   ],
   "checked_ms": 1794099400000,
   "spec": {
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
   },
   "created_by": "urn:li:corpuser:raj",
   "updated_ms": 1792890400000
  },
  {
   "dataset": "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.inventory.stock_levels,PROD)",
   "version": "1.0.3",
   "owner": "urn:li:corpuser:mei",
   "status": "BROKEN",
   "parts": [
    {
     "part": "schema",
     "status": "BROKEN",
     "detail": "column on_hand: promised INTEGER, found STRING"
    },
    {
     "part": "volume",
     "name": "volume",
     "monitor_id": "contract-4b7e0c-volume",
     "status": "MET",
     "detail": "1,204,331 rows within 900,000-1,500,000"
    }
   ],
   "checked_ms": 1794098500000,
   "spec": {
    "dataset": "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.inventory.stock_levels,PROD)",
    "version": "1.0.3",
    "owner": "urn:li:corpuser:mei",
    "description": "",
    "schema": {
     "columns": [
      {
       "name": "sku",
       "type": "VARCHAR",
       "required": true
      },
      {
       "name": "on_hand",
       "type": "INTEGER",
       "required": true
      }
     ],
     "allow_extra_columns": true
    },
    "volume": {
     "min_rows": 900000,
     "max_rows": 1500000,
     "learned": false
    },
    "quality": []
   },
   "created_by": "urn:li:corpuser:mei",
   "updated_ms": 1788916000000
  },
  {
   "dataset": "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.loyalty.members,PROD)",
   "version": "0.3.0",
   "owner": "urn:li:corpuser:tom",
   "status": "PENDING",
   "parts": [
    {
     "part": "quality",
     "name": "email_not_null",
     "monitor_id": "contract-7a2c91-email_not_null",
     "status": "PENDING",
     "detail": "not evaluated yet"
    }
   ],
   "checked_ms": 1794099880000,
   "spec": {
    "dataset": "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.loyalty.members,PROD)",
    "version": "0.3.0",
    "owner": "urn:li:corpuser:tom",
    "description": "Rewards members",
    "quality": [
     {
      "name": "email_not_null",
      "type": "field_metric",
      "config": {
       "field": "email",
       "metric": "null_count",
       "operator": "eq",
       "value": 0
      },
      "severity": "HIGH"
     }
    ]
   },
   "created_by": "urn:li:corpuser:tom",
   "updated_ms": 1794013600000
  }
 ],
 "/v1/passkeys": {
  "passkeys": [
   {
    "id": "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA",
    "name": "YubiKey 5C",
    "created_ms": 1786324000000,
    "last_used_ms": 1794089200000
   },
   {
    "id": "nW4kq2Lr8Vt0xY6pZc1bAeFgHiJkLmNoPqRsTuVwXyz",
    "name": "MacBook Touch ID",
    "created_ms": 1791508000000,
    "last_used_ms": 1793581600000
   },
   {
    "id": "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0FeDcBa-_12345",
    "name": "Pixel 9",
    "created_ms": 1793927200000,
    "last_used_ms": null
   }
  ]
 },
 "/v1/secret-scopes": [
  {
   "name": "payments-gateway",
   "owner": "urn:li:corpuser:raj",
   "description": "Adyen API credentials for refunds reconciliation",
   "created_ms": 1776820000000,
   "permission": "MANAGE"
  },
  {
   "name": "shopify-export",
   "owner": "urn:li:corpuser:tom",
   "description": "Shopify Admin API token",
   "created_ms": 1790212000000,
   "permission": "READ"
  },
  {
   "name": "weather-api",
   "owner": "urn:li:corpuser:mei",
   "description": "",
   "created_ms": 1793495200000,
   "permission": "WRITE"
  }
 ],
 "/v1/audit/siem": [
  {
   "sink": "splunk",
   "type": "splunk_hec",
   "seq": 1882043,
   "lag": 0,
   "sent": 1882043,
   "last_ms": 1794099996000,
   "failures": 0,
   "error": null,
   "retry_ms": null
  },
  {
   "sink": "qradar",
   "type": "syslog",
   "seq": 1881990,
   "lag": 53,
   "sent": 1881990,
   "last_ms": 1794099939000,
   "failures": 0,
   "error": null,
   "retry_ms": null
  },
  {
   "sink": "datadog",
   "type": "http",
   "seq": 1874511,
   "lag": 7532,
   "sent": 1874511,
   "last_ms": 1794089200000,
   "failures": 6,
   "error": "HTTP 503 from collector: Service Unavailable",
   "retry_ms": 1794100240000
  }
 ],
 "/v1/admin/limits": {
  "tenant_per_minute": 6000,
  "user_per_minute": 600,
  "users": {
   "urn:li:corpuser:svc-dbt": 3000,
   "urn:li:corpuser:tom": 1200
  },
  "ip_allowlist": [
   "10.0.0.0/8",
   "203.0.113.0/24"
  ],
  "trusted_proxies": [
   "10.42.0.0/16"
  ]
 },
 "/workspaces": [
  {
   "id": "stores",
   "name": "Store Operations",
   "domains": [
    "urn:li:domain:stores",
    "urn:li:domain:inventory"
   ],
   "role": "admin"
  },
  {
   "id": "marketing",
   "name": "Marketing & Loyalty",
   "domains": [
    "urn:li:domain:loyalty",
    "urn:li:domain:marketing"
   ],
   "role": "member"
  },
  {
   "id": "finance",
   "name": "Finance",
   "domains": [
    "urn:li:domain:finance"
   ],
   "role": "global_admin"
  }
 ],
 "/v1/shares": {
  "shares": [
   {
    "id": "8d0f2b4d6f8a40c2e4a6c8e0a2c4e6a8",
    "name": "acme-logistics-shipments",
    "recipient": "Acme Logistics",
    "tables": [
     "supply.shipments",
     "supply.stock_levels"
    ],
    "created_by": "urn:li:corpuser:alice",
    "created_at": 1793236000,
    "expires_at": 1795828000,
    "revoked_at": null,
    "revoked_by": null,
    "last_used_at": 1794096800,
    "region": "eu",
    "state": "active"
   },
   {
    "id": "2f4b6d8f0a2c44e6a8c0e2a4c6e8a0c2",
    "name": "agency-q3-campaign",
    "recipient": "Brightline Media",
    "tables": [
     "marketing.campaign_results"
    ],
    "created_by": "urn:li:corpuser:mei",
    "created_at": 1785892000,
    "expires_at": 1793668000,
    "revoked_at": null,
    "revoked_by": null,
    "last_used_at": 1793581600,
    "region": null,
    "state": "expired"
   },
   {
    "id": "b6d8f0a2c4e648a0c2e4a6c8e0a2c4e6",
    "name": "supplier-sell-through",
    "recipient": "Fresh Farms Co-op",
    "tables": [
     "sales.sell_through_by_supplier"
    ],
    "created_by": "urn:li:corpuser:raj",
    "created_at": 1790644000,
    "expires_at": 1798420000,
    "revoked_at": 1793927200,
    "revoked_by": "urn:li:corpuser:alice",
    "last_used_at": 1793840800,
    "region": null,
    "state": "revoked"
   }
  ]
 },
 "/marketplace/slos": [
  {
   "id": "inventory-quality",
   "product_urn": "urn:li:dataProduct:inventory",
   "name": "Inventory quality checks pass",
   "kind": "quality",
   "objective": 99.5,
   "window_days": 28,
   "description": "",
   "datasets": [
    "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.inventory.stock_levels,PROD)"
   ],
   "monitors": 4,
   "good": 1068,
   "total": 1092,
   "sli": 97.802,
   "budget_remaining": 0.0,
   "burn_rate": 5.6,
   "state": "breached"
  },
  {
   "id": "orders-freshness",
   "product_urn": "urn:li:dataProduct:sales-orders",
   "name": "Orders fresh within 6h",
   "kind": "freshness",
   "objective": 99.0,
   "window_days": 28,
   "description": "For store dashboards",
   "datasets": [
    "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.sales.orders,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.sales.order_lines,PROD)"
   ],
   "monitors": 2,
   "good": 1336,
   "total": 1344,
   "sli": 99.405,
   "budget_remaining": 40.5,
   "burn_rate": 0.5,
   "state": "met"
  },
  {
   "id": "loyalty-volume",
   "product_urn": "urn:li:dataProduct:loyalty",
   "name": "Loyalty daily volume",
   "kind": "volume",
   "objective": 95.0,
   "window_days": 14,
   "description": "",
   "datasets": [
    "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.loyalty.members,PROD)"
   ],
   "monitors": 0,
   "good": 0,
   "total": 0,
   "sli": null,
   "budget_remaining": null,
   "burn_rate": null,
   "state": "no_data"
  }
 ],
 "/ai/usage": {
  "groups": [
   {
    "key": "assistant",
    "calls": 1842,
    "input_tokens": 9312450,
    "output_tokens": 612380,
    "cost_usd": 37.122815
   },
   {
    "key": "gateway:store-ops-helper",
    "calls": 655,
    "input_tokens": 2104330,
    "output_tokens": 188020,
    "cost_usd": 9.133292
   },
   {
    "key": "docgen",
    "calls": 214,
    "input_tokens": 840112,
    "output_tokens": 96540,
    "cost_usd": 3.968436
   }
  ]
 },
 "/v1/budgets": [
  {
   "id": "6f1c2e9a0b7d4c13a85e2f90d4b17c66",
   "name": "AI spend",
   "amount_usd": 2000.0,
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
    "urn:li:corpGroup:finops",
    "urn:li:corpuser:tom"
   ],
   "enabled": true,
   "created_by": "urn:li:corpuser:alice",
   "created_ms": 1786324000000,
   "checked_ms": 1794098800000,
   "spent_usd": 1342.57
  },
  {
   "id": "b2d47e0c19a54f8e9c3a7d61e05f2b48",
   "name": "Sales domain storage and compute",
   "amount_usd": 8500.0,
   "period": "month",
   "scope": {
    "domain": "urn:li:domain:sales"
   },
   "thresholds": [
    50,
    80,
    100
   ],
   "forecast": true,
   "notify": [
    "urn:li:corpuser:raj"
   ],
   "enabled": true,
   "created_by": "urn:li:corpuser:raj",
   "created_ms": 1790212000000,
   "checked_ms": 1794098800000,
   "spent_usd": 7910.2
  },
  {
   "id": "0d9e3a71c6b2481f97e5a4c02b8d6f13",
   "name": "BI warehouse",
   "amount_usd": 300.0,
   "period": "week",
   "scope": {
    "warehouse": "bi-medium"
   },
   "thresholds": [
    100
   ],
   "forecast": false,
   "notify": [],
   "enabled": false,
   "created_by": "urn:li:corpuser:mei",
   "created_ms": 1793840800000,
   "checked_ms": null,
   "spent_usd": null
  }
 ],
 "/v1/ai/gateway/models": {
  "data": [
   {
    "id": "claude-sonnet-4-5",
    "object": "model"
   },
   {
    "id": "claude-haiku-4-5",
    "object": "model"
   },
   {
    "id": "gpt-4o-mini",
    "object": "model"
   }
  ]
 },
 "/v1/chargeback/statements": {
  "statements": [
   {
    "id": "4e1d9c0b7a2f4e8d9c3b1a0f6e5d4c3b",
    "month": "2026-10",
    "version": 2,
    "issued_by": "urn:li:corpuser:alice",
    "issued_ms": 1793927200000,
    "total_usd": "48213.77"
   },
   {
    "id": "a7c3e5f1b9d24c6e8a0b2d4f6e8c0a1b",
    "month": "2026-10",
    "version": 1,
    "issued_by": "urn:li:corpuser:alice",
    "issued_ms": 1793581600000,
    "total_usd": "47988.12"
   },
   {
    "id": "0f2e4d6c8b1a3e5d7c9b0a2f4e6d8c1a",
    "month": "2026-09",
    "version": 1,
    "issued_by": "urn:li:corpuser:raj",
    "issued_ms": 1790903200000,
    "total_usd": "45102.40"
   }
  ]
 },
 "/v1/chargeback/cost-centres": {
  "cost_centres": [
   {
    "code": "CC-4100",
    "name": "Store Operations",
    "teams": [
     "urn:li:corpGroup:store-ops"
    ],
    "updated_by": "urn:li:corpuser:alice",
    "updated_ms": 1789780000000
   },
   {
    "code": "CC-5200",
    "name": "Marketing",
    "teams": [
     "urn:li:corpGroup:marketing",
     "urn:li:corpGroup:loyalty"
    ],
    "updated_by": "urn:li:corpuser:alice",
    "updated_ms": 1792372000000
   },
   {
    "code": "CC-6300",
    "name": "Supply Chain Analytics",
    "teams": [
     "urn:li:corpGroup:supply-chain"
    ],
    "updated_by": "urn:li:corpuser:raj",
    "updated_ms": 1793840800000
   }
  ]
 }
};

/* Sample answers to the areas' actions that return something to read, keyed "<METHOD> <path pattern>" (":id" is one
   path segment), in the API's own shapes; fictional data. Other actions are accepted without running. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const ACTION_SAMPLES: Record<string, any> = {
 "POST /v1/vectors/:id/answer": {
  "index": "support-tickets",
  "question": "How long do refunds take?",
  "method": "exact",
  "retrieved": 8,
  "answered": true,
  "answer": "Refunds are issued within 30 days of purchase when the item is returned unused. [1] Card refunds reach the account in three to five business days. [3] Most customers get their money back within a week.",
  "sentences": [
   {"text": "Refunds are issued within 30 days of purchase when the item is returned unused.", "sources": [1], "supported": true},
   {"text": "Card refunds reach the account in three to five business days.", "sources": [3], "supported": true},
   {"text": "Most customers get their money back within a week.", "sources": [], "supported": false}
  ],
  "unsupported": [2],
  "citations": [
   {"n": 1, "key": "T-10482", "chunk": 0, "text": "Refunds are issued within 30 days of purchase when the item is returned unused. Store credit is offered after that.", "score": 0.8312},
   {"n": 3, "key": "T-11907", "chunk": 1, "text": "Once approved, card refunds reach the customer's account in three to five business days, depending on the bank.", "score": 0.7744}
  ],
  "refusal": null,
  "model": "support-answers-model"
 }
};
