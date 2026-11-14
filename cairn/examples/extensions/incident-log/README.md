# Incident log: an example Cairn extension

It shows the three parts of the extension contract:

- **Events:** Cairn posts quality and contract events here, signed with the extension's secret (`Cairn-Signature`). Unsigned, altered or stale deliveries are refused.
- **A scoped token:** for each event it reads the asset's trust badges through Cairn's API. The token can do only what `permissions` lists: here, `VIEW_ENTITY_PAGE`.
- **Workspace sections:** a page under **Extensions**, and a tab on every asset page (`?urn=` filters to that asset). Both run in a sandboxed frame.

To run it next to a Compose deployment:

```bash
cairn extension install examples/extensions/incident-log      # prints the token and secret once
docker run -d --name incident-log --network cairn_catalog -p 8098:8098 \
  -e CAIRN_API_URL=http://cairn-api:8800 -e CAIRN_EXTENSION_TOKEN=cx_… -e CAIRN_SIGNING_SECRET=… \
  -v "$PWD/examples/extensions/incident-log:/app:ro" python:3.12-slim python /app/app.py
```
