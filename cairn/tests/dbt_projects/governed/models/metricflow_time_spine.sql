{{ config(materialized='table') }}
select cast(d as date) as date_day from unnest(sequence(date '2026-01-01', date '2026-12-31', interval '1' day)) as t(d)
