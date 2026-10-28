---
title: Marketplace
order: 4
---
# Marketplace

The marketplace lists **data products**: curated sets of assets with an owner, a description,
quality status and the number of consumers.

## Requesting access

1. Open a product and choose **Request access**. Pick a purpose and, if offered, a duration.
2. Cairn shows what you would get before you submit (`POST /cairn/api/marketplace/access-requests/preview`).
3. The product's **request policy** decides what happens next: automatic approval for eligible
   requesters, or approval by the product's approvers. Requesters can never approve their own
   request.
4. Approvers decide in the workspace or with one click in Slack, Teams or email. Approved access is
   granted immediately as a persona grant; time-boxed grants are revoked automatically when they
   expire.

## Publishing a product

Product owners set the request policy (`PUT /cairn/api/marketplace/products/{urn}/policy`): who may
request, which purposes are allowed, the maximum duration and the approvers. Consumers can subscribe
to a product to hear about incidents and changes.

## Sharing outside the company

A data product can be offered to other companies as a **Snowflake listing** or a **Databricks Delta Sharing share**. Cairn builds it from what the catalog already knows, so nobody types the product up twice.

On the product page, an owner or admin picks the target and enters:

- a share name,
- for Snowflake, the consumer accounts (`ORG.ACCOUNT`) and the listing terms,
- for Databricks, the recipients.

**Plan** shows exactly what would run:

- **Snowflake:** a share that grants the product's Snowflake tables, and an external listing. Its manifest carries the product's description, data contracts, refresh rate, featured tables and up to three sample queries mined from real use of those tables.
- **Databricks:** a share with the product's Databricks tables and their descriptions, plus grants to recipients.

Tables on other platforms are listed as skipped. **Publish** runs the statements through a named warehouse connection, the same ones tag sync uses. With no connection, it records that you ran them yourself. Snowflake listings are created unpublished (`PUBLISH = FALSE`): review the listing in Snowflake before you release it.

**Kept in sync:** Cairn keeps a fingerprint of what it published. When the product changes (a table added or removed, a description, a contract), the owners get a notification. The publication then shows as out of date until it is published again. Republishing adds and removes tables to match, and replaces the listing's manifest (`ALTER LISTING`).

The same is available as an API: `POST /cairn/api/marketplace/products/<urn>/publish/<snowflake|databricks>/plan` and `…/publish/<target>`. `GET /cairn/api/marketplace/publications` lists everything published.
