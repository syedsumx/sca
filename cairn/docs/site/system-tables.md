---
title: System tables
order: 2.5
---
# System tables: Cairn's own data in SQL

Cairn keeps its own operational data as tables you can query in Trino, in the catalog `cairn_system`:
who did what, which reads were refused, which queries ran and what they cost, lineage, and job runs.
Query them from the SQL workspace, a notebook, a BI tool or `cairn sql`, like any other table.

In the SQL workspace, the **System tables** list under the table browser shows each one, with its columns.
Double-click a name to insert it.

| Table | What it holds |
|---|---|
| `cairn_system.audit.events` | The audit log: every change, sign-in and export, in order, with its hash chain |
| `cairn_system.access.decisions` | Access the policy engine refused |
| `cairn_system.query.history` | Queries Trino ran: who, when, how long, bytes, tables, warehouse, price |
| `cairn_system.billing.usage` | Costs per day: your queries, tables, teams, engine runs, AI usage |
| `cairn_system.lineage.table_lineage` | Each table and the tables it is made from |
| `cairn_system.lineage.column_lineage` | Each column and the columns it is computed from |
| `cairn_system.compute.warehouse_events` | Team warehouses: started by hand, and node-seconds per period |
| `cairn_system.jobs.runs` | Runs of flows, scheduled jobs and SQL workflows |

## What you see

- **Your own rows.** In the tables with a `user_name` column (queries, costs, runs, the audit log, refused
  access), you see your own rows. Operators see everyone's.
- **SQL text is masked.** `statement` and `error` in `query.history` show `••••` unless you hold the
  `VIEW_QUERY_TEXT` privilege, even for your own queries: SQL can hold values people typed. To find the same
  statement again, use `statement_hash` (the same SQL has the same hash) or `fingerprint` (the same query with
  other values has the same fingerprint).
- **Access.** Your administrator grants the system tables like any table. Without that grant they are hidden
  and a query on them is refused.
- **Freshness.** Rows arrive within two minutes (the sync interval plus a minute). `synced_at` on every row
  says when it arrived.
- **How long they keep.** 90 days by default; your administrator can change it per table.

## Examples

My most expensive queries this week:

```sql
SELECT created_at, cost_usd, wall_ms, input_bytes, warehouse, statement_hash
FROM cairn_system.query.history
WHERE created_at >= date_trunc('week', current_timestamp)
ORDER BY cost_usd DESC NULLS LAST
LIMIT 10
```

What I spent each day this month, by kind (queries, AI):

```sql
SELECT day, kind, sum(cost_usd) AS usd
FROM cairn_system.billing.usage
WHERE day >= date_trunc('month', current_date)
GROUP BY day, kind
ORDER BY day, kind
```

The queries I run most often, and how long they take:

```sql
SELECT fingerprint, count(*) AS runs, approx_percentile(wall_ms, 0.5) AS median_ms
FROM cairn_system.query.history
WHERE created_at >= current_timestamp - INTERVAL '30' DAY
GROUP BY fingerprint
ORDER BY runs DESC
LIMIT 10
```

My failed runs in the last week:

```sql
SELECT kind, name, started_at, error
FROM cairn_system.jobs.runs
WHERE status = 'failed' AND created_at >= current_timestamp - INTERVAL '7' DAY
ORDER BY started_at DESC
```

What a table is made from, column by column:

```sql
SELECT downstream_column, upstream_urn, upstream_column
FROM cairn_system.lineage.column_lineage
WHERE downstream_urn = 'urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.by_region,PROD)'
```

What changed since I last looked (the change feed):

```sql
SELECT * FROM cairn_system.audit.events
WHERE synced_at > TIMESTAMP '2026-11-09 08:00:00 UTC'
ORDER BY seq
```

The tables are read-only: nothing can be written, changed or dropped through Trino.
