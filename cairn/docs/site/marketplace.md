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
