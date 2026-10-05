"""Job worker: ``tether worker`` (or a thread inside the API with TETHER_RUN_WORKER_IN_API)."""

import logging
import threading
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from tether.config import get_settings
from tether.db.session import get_session_factory, session_scope
from tether.deletion import queue, service

log = logging.getLogger("tether.worker")

Handler = Callable[[dict[str, Any], sessionmaker[Session]], Any]

HANDLERS: dict[str, Handler] = {
    service.JOB_KIND: lambda payload, factory: service.process(payload["request_id"], factory),
}


def run_once(factory: sessionmaker[Session] | None = None) -> bool:
    """Claim and run one job. Returns False when the queue is empty."""
    factory = factory or get_session_factory()
    with session_scope(factory) as session:
        job = queue.claim(session)
        if job is None:
            return False
        job_id, kind, payload = job.id, job.kind, dict(job.payload)
    log.info("running job %s (%s)", job_id, kind)
    try:
        handler = HANDLERS[kind]
        handler(payload, factory)
    except Exception as exc:
        with session_scope(factory) as session:
            retried = queue.fail(session, job_id, f"{type(exc).__name__}: {exc}")
        log.warning("job %s failed (%s); retry scheduled: %s", job_id, exc, retried)
        return True
    with session_scope(factory) as session:
        queue.complete(session, job_id)
    return True


def drain(factory: sessionmaker[Session] | None = None, max_jobs: int = 1000) -> int:
    """Run jobs until the queue is empty (used by tests and the in-process demo)."""
    n = 0
    while n < max_jobs and run_once(factory):
        n += 1
    return n


def run_forever(stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    interval = get_settings().worker_poll_interval
    log.info("worker started (poll interval %.1fs)", interval)
    while not stop.is_set():
        try:
            if not run_once():
                stop.wait(interval)
        except Exception:  # keep the worker alive on infrastructure hiccups
            log.exception("worker loop error")
            stop.wait(interval * 4)


def start_background_worker() -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(target=run_forever, args=(stop,), name="tether-worker", daemon=True)
    thread.start()
    return thread, stop
