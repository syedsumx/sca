{{ config(materialized='table') }}
select kind, count(*) as events, max(loaded_at) as last_loaded from {{ source('sales', 'events') }} group by kind
