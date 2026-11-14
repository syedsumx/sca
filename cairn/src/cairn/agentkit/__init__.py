"""Cairn's agent kit: code-first agents, from an empty directory to a deployed, governed agent.

``cairn agents init`` makes a project (``agent.py``, ``agent.toml``, tests, a chat page), ``cairn agents dev`` runs it on
your machine with your own Cairn token, and ``cairn agents deploy`` sends it to Cairn, which runs it as an agent app:
an agent identity of its own, a session store, invoked through Cairn as the person calling it.

A project's ``agent.py`` defines ``respond(ctx, messages)``: ``messages`` is the conversation so far (``{"role",
"content"}`` dicts, the newest last) and ``ctx`` (:class:`cairn.agentkit.runtime.Context`) gives the model clients,
already pointed at Cairn's AI gateway with the right token (``ctx.anthropic()``, ``ctx.openai()``), governed SQL
(``ctx.sql(...)``), who is asking (``ctx.user``) and the session (``ctx.session_id``). It returns the answer's text.
"""
