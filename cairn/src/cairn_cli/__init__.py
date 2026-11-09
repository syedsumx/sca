"""The Cairn command line for people: sign in (``cairn login``), then query, search, show and ask as yourself.

This package needs only the client libraries (httpx, PyYAML, keyring): it never imports Cairn's server code, so it
installs light (``pipx install cairn-cli``) and builds into a single binary. The full ``cairn-platform`` install has
the same commands beside the operators' ones (:mod:`cairn.cli`).
"""

__version__ = "0.1.0"
