"""Exit codes, the same for every command (documented in docs/site/command-line.md)."""

from __future__ import annotations

OK, ERROR, USAGE, AUTH, DENIED, NOT_FOUND = 0, 1, 2, 3, 4, 5
INTERRUPTED = 130

EXIT_CODES = {
    OK: "success",
    ERROR: "an error (the request failed, Cairn could not be reached, bad input to Cairn)",
    USAGE: "usage: unknown command or option, missing argument",
    AUTH: "not signed in, or the sign-in expired or was revoked: run `cairn login`",
    DENIED: "permission denied: Cairn refused the request as you (the message says which privilege on what)",
    NOT_FOUND: "not found: no such asset, flow, profile or query",
    INTERRUPTED: "interrupted (Ctrl-C); a running query is cancelled in Cairn first",
}


class CliError(Exception):
    """Ends the command with a message on standard error and an exit code. Never carries a token."""

    def __init__(self, message: str, code: int = ERROR):
        super().__init__(message)
        self.code = code


def for_status(status: int) -> int:
    """The exit code for an HTTP status from Cairn."""
    if status == 401:
        return AUTH
    if status == 403:
        return DENIED
    if status in (404, 410):
        return NOT_FOUND
    return ERROR
