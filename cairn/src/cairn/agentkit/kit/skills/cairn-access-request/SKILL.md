---
name: cairn-access-request
description: "Get access to data in Cairn the right way: check what is allowed, find the purpose that fits, and file a request its owner can approve. Use when a query or tool call is refused, or someone asks how to get access to a dataset."
---

# Access to data in Cairn

A refusal is Cairn's policy speaking. Never look for another copy of the data, another connection or another
identity to get around it.

1. **What is allowed now.** `check_access` with the privilege and the asset's URN (for example `VIEW_DATASET_PROFILE`);
   `list_sensitive_fields` for the columns that are masked or need a purpose.
2. **Is there something that already fits?** A data product or a share built for this use (`search_assets`), or an
   aggregate without the sensitive columns.
3. **Ask.** `request_access` takes the data product's URN, not a table's; a `purpose` the product allows; a
   `justification` of at least a sentence; and a duration in days within the product's maximum. It has no column or
   read/write option: name the columns needed in the justification, and ask for the shortest duration that does the
   job. Say who decides: the product's approvers.
4. **Meanwhile**, continue with what is allowed: masked values, aggregates, sample data. Report plainly what is
   waiting on the request.
