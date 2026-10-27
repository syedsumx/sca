---
title: Data quality
order: 2
---
# Data quality

Monitors check datasets on a schedule. Results appear on the asset's **Quality** tab and as
assertions in the catalog; repeated failures open an **incident** that followers are notified about
and that closes automatically when the data recovers.

## Monitor types

| Type | Checks | Source |
|---|---|---|
| `freshness` | time since the last update | catalog metadata, or SQL with `timestamp_column` |
| `volume` | row count and its change | catalog metadata or SQL |
| `field_metric` | one column metric: `null_pct`, `distinct_pct`, `mean`, `min`, `max`, `stddev`, `avg_length`, `row_count` | catalog or SQL |
| `distribution` | drift of a column's value distribution | SQL |
| `schema` | added, removed and retyped columns | catalog |
| `custom_sql` | any read-only query that returns a number | SQL (write statements are rejected) |

SQL sources run through a named connection that your administrator configures; credentials never
appear in monitor definitions.

## Learned thresholds

New monitors start in `TRAINING`: they learn the normal range from history, then switch to `ACTIVE`.
`sensitivity` (`low`, `medium`, `high`) sets how far a value may move before it fails. Give
feedback on a failure: **expected** (a legitimate change) keeps the value in the baseline so the
monitor adapts; **real** keeps it out so one incident does not shift the normal range.

## Severity and incidents

`severity` (`LOW` … `CRITICAL`) ranks incidents. `incident_after_failures` sets how many consecutive
failures raise one (default 1). Optional `notify` webhooks receive every incident in addition to the
followers' own channels.

## Monitors by rule

Instead of adding monitors one dataset at a time, write rules that keep monitors on every dataset
they select: every certified dataset, every table in the Finance domain, every column classified as
personal data. Rules use the same conditions as access policies and live in YAML files in
`config/quality/` (Kubernetes: `cairn.quality.autoMonitors` in the chart's values):

```yaml
rules:
  - id: certified
    select: {attr: resource.certification, op: eq, value: CERTIFIED}
    monitors:
      - {type: freshness, severity: HIGH}
      - {type: volume, severity: HIGH}
  - id: pii-nulls
    select: {attr: resource.classification, op: contains, value: PII}
    monitors:
      - {type: field_metric, fields: {labelled: [PII]}, config: {metric: null_pct, max: 5}}
```

Every 10 minutes the rules are applied: newly matching datasets get the monitors, changed rules
update them, and datasets that no longer match lose them. Monitors people created are never
touched. Editing a rule's monitor takes it out of the rule's hands; deleting one disables it and
keeps the rule from recreating it. Preview a change with
`POST /cairn/api/quality/auto/sweep?dry_run=true`, apply it now without `dry_run`.

## Profiling

For tables whose ingestion does not collect statistics, a rule can profile them over a SQL
connection (`CAIRN_QUALITY_CONNECTIONS`, read-only role):

```yaml
  - id: warehouse
    select: {attr: resource.platform, op: eq, value: postgres}
    profile: {connection: warehouse, every_hours: 24}
```

A profile (row count; per column null and distinct counts; minimum, maximum, mean and standard
deviation for numbers) is written to the catalog when the latest one is older than `every_hours`,
so it shows on the asset's statistics and feeds volume and column monitors. Sample values are off
unless `sample_values` is set; when on, they are masked like any other.

## Example

```json
POST /cairn/api/quality/monitors
{
  "name": "orders arrive hourly",
  "dataset_urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,sales.orders,PROD)",
  "type": "freshness",
  "schedule": {"interval_seconds": 900},
  "severity": "HIGH"
}
```

`POST /cairn/api/quality/monitors/{id}/run` evaluates immediately;
`GET /cairn/api/quality/datasets/{urn}/health` summarises a dataset.
