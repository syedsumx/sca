---
name: cairn-access-request
description: "Get access to data in Cairn the right way: check what is allowed, find the purpose that fits, and file a request its owner can approve. Use when a query or tool call is refused, or someone asks how to get access to a dataset."
---

# Access to data in Cairn

A refusal is Cairn's policy speaking. Never look for another copy of the data, another connection or another
identity to get around it.

1. **What is allowed now.** `check_access` on the asset for the person; `list_sensitive_fields` for the columns that
   are masked or need a purpose.
2. **Is there something that already fits?** A data product or a share built for this use (`search_assets`), or an
   aggregate without the sensitive columns.
3. **Ask.** `request_access` with the asset, the least access that does the job (read, not write; the columns
   needed, not all), how long it is needed, and the purpose in one honest sentence. Say who approves it.
4. **Meanwhile**, continue with what is allowed: masked values, aggregates, sample data. Report plainly what is
   waiting on the request.
