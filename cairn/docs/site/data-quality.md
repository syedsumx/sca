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
