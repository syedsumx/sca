# Cairn clients

Clients for the main Cairn APIs, without dependencies. How to use them, and the API deprecation policy they follow,
are in the API docs (`docs/site/api.md`, *Clients* and *Deprecation policy*).

| Client | Where | Checks |
|---|---|---|
| JavaScript / TypeScript (Node 18+, browsers) | `js/` (`@cairn/client`) | `node --test test/client.test.mjs` offline; live against Cairn from `tests/test_sdk_clients.py` |
| Java 17+ | `java/` (`io.cairn.client`) | `javac -d out $(find src -name '*.java') && java -cp out io.cairn.client.ClientCheck` offline; live from `tests/test_sdk_clients.py` |
