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
`sensitivity` (`low`, `medium`, `high`) sets how far a value may move before it fails.

### The learned model

Set `config.model: learned` on `volume`, `field_metric` and `custom_sql` monitors. Monitors
created in the editor use it by default. The learned model differs from the default baseline in four ways:

- **It learns before it alerts.** The monitor stays in `TRAINING` until it has `learn_days` of
  history (default 21) and at least `min_history` points (default 14). Hard bounds such as
  `min_rows` or `max` still fail right away.
- **It follows the trend.** A robust slope (Theil–Sen) is fitted on the history, and past values
  are projected to today. A table that grows by 500 rows a day therefore isn't flagged for
  growing, and a sudden drop still is.
- **It picks the season.** With `seasonality: auto`, the model compares a value with the same hour
  of the week once there are two weeks of sub-daily points. After three days of sub-daily points it
  compares with the same hour of the day. After three weeks of daily points it compares with the
  same weekday. Otherwise it compares with all recent points. You can also name a cycle
  (`hour_of_week`, `day_of_week`, `hour_of_day`) or set none.
- **The band is adjustable.** `band` is the band's half-width in robust standard deviations, from 1
  to 10. It replaces the sensitivity preset (low 4, medium 3, high 2.5). Values beyond 75% of the
  band are a `WARN`.

Each evaluation records the expected range, the cycle used and the trend per day. The chart draws
the expected range as a band.

### Feedback

Give feedback on any failure or warning:

- **Expected:** a legitimate value. It stays in the baseline so the monitor adapts.
- **Real:** an incident. It is kept out of the baseline, so one bad day doesn't shift the normal range.
- **New normal:** an intended step change, such as a backfill or a new source. The baseline
  restarts from this value. The monitor needs `min_history` fresh points, but not another learning
  period.

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
