"""Keeping the API's secrets from the code it starts beside itself.

Cairn starts some processes as children of the API, as the API's own OS user: ``command`` data apps, and where the
operator allows it, installed app packages, process-runtime agent apps and process-runtime model servers. Their
environment is trimmed, but a process of the same user can read another's ``/proc/<pid>/environ`` and ``mem``,
where the API keeps ``CAIRN_SERVICE_TOKEN``, ``CAIRN_APPS_SECRET`` and the database URL.

* :func:`make_undumpable`: the API marks itself not dumpable (``prctl(PR_SET_DUMPABLE, 0)``) when it starts, so
  the kernel refuses same-user processes its ``/proc/<pid>/environ``, ``mem`` and ``ptrace``. (No core dumps either.)
* :func:`local_untrusted_code_allowed`: another party's code (app package installs, agent apps) runs beside the API
  only when the operator says so with ``CAIRN_ALLOW_LOCAL_UNTRUSTED_CODE=true``; otherwise it needs the Kubernetes
  runtime, where it runs in a pod of its own as another user.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys

logger = logging.getLogger(__name__)

PR_SET_DUMPABLE, PR_GET_DUMPABLE = 4, 3
LOCAL_UNTRUSTED = "CAIRN_ALLOW_LOCAL_UNTRUSTED_CODE"


def _prctl():
    return ctypes.CDLL(None, use_errno=True).prctl


def make_undumpable() -> bool:
    """Marks this process not dumpable; True when it is (Linux only)."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        prctl = _prctl()
        if prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE)")
    except (OSError, AttributeError) as e:
        logger.warning("could not mark the API not dumpable (processes it starts may read its environment): %s", e)
        return False
    return True


def dumpable() -> bool:
    """Whether this process is dumpable (always True off Linux)."""
    if not sys.platform.startswith("linux"):
        return True
    return _prctl()(PR_GET_DUMPABLE, 0, 0, 0, 0) != 0


def local_untrusted_code_allowed() -> bool:
    """Whether the operator lets another party's code run beside the API as its OS user."""
    return os.environ.get(LOCAL_UNTRUSTED, "").strip().lower() in ("1", "true", "yes")
