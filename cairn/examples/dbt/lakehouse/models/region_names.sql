{{ config(tags=['reference']) }}
select region, name from {{ ref('regions') }}
