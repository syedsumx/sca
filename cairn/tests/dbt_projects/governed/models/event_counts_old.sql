{{ config(materialized='view') }}
select kind, events from {{ ref('event_counts') }}
