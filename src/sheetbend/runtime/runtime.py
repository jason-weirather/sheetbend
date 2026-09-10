"""Host-local admission control and bounded content-free activity inspection."""

from collections.abc import Mapping
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import sqlite3
from threading import Event, Lock
import time
from typing import Any
import uuid

from ..errors import ConnectionClosedError, CoordinationError
from .process import alive, caller_labels, identity
from .storage import Store

_ACTIVE = ("waiting", "running", "streaming")
_TERMINAL = ("done", "error", "cancelled", "abandoned")
_WINDOW_SECONDS = 60.0
_RECENT_SECONDS = 600.0
_RECENT_LIMIT = 1000
_POLL_SECONDS = 0.1


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _maintain(db: sqlite3.Connection, now: float, tick: float) -> None:
    """Reap only demonstrably dead processes; a paused live process keeps its slot."""
    processes = db.execute(
        "SELECT DISTINCT pid, process_started FROM requests WHERE state IN (?, ?, ?)", _ACTIVE
    ).fetchall()
    for process in processes:
        if not alive(process["pid"], process["process_started"]):
            db.execute(
                "UPDATE requests SET state='abandoned', finished_at=?, finished_tick=? "
                "WHERE pid=? AND process_started=? AND state IN (?, ?, ?)",
                (now, tick, process["pid"], process["process_started"], *_ACTIVE),
            )
    db.execute("DELETE FROM starts WHERE tick <= ?", (tick - _WINDOW_SECONDS,))
    db.execute("DELETE FROM requests WHERE finished_tick < ?", (tick - _RECENT_SECONDS,))
    db.execute(
        "DELETE FROM requests WHERE request_id IN (SELECT request_id FROM requests "
        "WHERE finished_tick IS NOT NULL ORDER BY finished_tick DESC, request_id LIMIT -1 OFFSET ?)",
        (_RECENT_LIMIT,),
    )
    db.execute(
        "DELETE FROM buckets WHERE id NOT IN (SELECT bucket FROM requests) "
        "AND id NOT IN (SELECT bucket FROM starts)"
    )


class Runtime:
    """Inspect this user's Sheetbend activity on this host, including other processes.

    Construction creates no files. snapshot() reads a fresh structured view and
    reaps dead processes if a ledger exists. It never reads prompts, responses,
    argv, resolved credentials, or user input paths. Active rows are retained;
    completed history is bounded to 1,000 entries and ten minutes. Runtime state
    must be on a local filesystem, not NFS or another shared home filesystem.
    """

    def __init__(self, directory: str | os.PathLike[str] | None = None) -> None:
        self._store = Store(directory)

    @property
    def directory(self) -> Path:
        return self._store.directory

    def snapshot(self) -> dict[str, Any]:
        """Return an independent activity-schema document. No configuration is read."""
        now, tick = time.time(), time.monotonic()
        rows = []
        with self._store.transaction(create=False) as db:
            if db is not None:
                _maintain(db, now, tick)
                records = db.execute(
                    "SELECT * FROM requests ORDER BY (finished_tick IS NOT NULL), "
                    "CASE WHEN finished_tick IS NULL THEN created_tick ELSE -finished_tick END"
                ).fetchall()
                for record in records:
                    row = dict(record)
                    end = row["finished_tick"] if row["finished_tick"] is not None else tick
                    wait_end = row["started_tick"] if row["started_tick"] is not None else end
                    row["age_seconds"] = max(0, end - row["created_tick"])
                    row["wait_seconds"] = max(0, wait_end - row["created_tick"])
                    row["duration_seconds"] = (
                        max(0, end - row["started_tick"])
                        if row["started_tick"] is not None else None
                    )
                    for field in ("bucket", "created_tick", "started_tick", "finished_tick"):
                        row.pop(field)
                    rows.append(row)
        return {"schema_version": 1, "host": socket.gethostname(), "observed_at": now,
                "requests": rows}

    def __repr__(self) -> str:
        return f"Runtime(directory={str(self.directory)!r}, scope='same-user/same-host')"


class _Request:
    """One acquired request. Completion is idempotent and preserves unknown usage."""

    def __init__(self, store: Store, request_id: str) -> None:
        self._store = store
        self.request_id = request_id
        self._finished = False
        self._lock = Lock()

    def streaming(self) -> None:
        with self._lock:
            if self._finished:
                return
            with self._store.transaction() as db:
                result = db.execute(
                    "UPDATE requests SET state='streaming' WHERE request_id=? AND state='running'",
                    (self.request_id,),
                )
                if result.rowcount != 1:
                    raise CoordinationError("Active request record was lost; stopping the request.")

    def finish(
        self, state: str, *, input_tokens: int | None = None, output_tokens: int | None = None
    ) -> None:
        if state not in _TERMINAL:
            raise ValueError("Invalid terminal request state.")
        with self._lock:
            if self._finished:
                return
            # Providers can omit usage. Invalid values remain unknown, never fabricated.
            counts = [v if type(v) is int and v >= 0 else None for v in (input_tokens, output_tokens)]
            with self._store.transaction() as db:
                result = db.execute(
                    "UPDATE requests SET state=?, finished_at=?, finished_tick=?, "
                    "input_tokens=?, output_tokens=? WHERE request_id=? AND finished_tick IS NULL",
                    (state, time.time(), time.monotonic(), *counts, self.request_id),
                )
                if result.rowcount != 1:
                    raise CoordinationError("Request record was lost; runtime accounting failed.")
            self._finished = True

    def __enter__(self) -> "_Request":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        state = "done" if exc_type is None else (
            "cancelled" if issubclass(exc_type, (KeyboardInterrupt, GeneratorExit)) else "error"
        )
        self.finish(state)


class _Admission:
    """The source's validated definition and a shared bucket; no persistent connection."""

    def __init__(self, namespace: str, name: str, definition: Mapping[str, Any]) -> None:
        self.registry_id = _digest(namespace)[:16]
        self.bucket = _digest([namespace, name])
        self.name = name
        # Model declarations do not change the shared budget; connection identity does.
        fields = ("base_url", "auth", "scope", "organization", "rate_limit")
        self.fingerprint = _digest({key: definition.get(key) for key in fields})
        self.limits = dict(definition["rate_limit"])
        self._runtime: Runtime | None = None

    @property
    def runtime(self) -> Runtime:
        if self._runtime is None:
            self._runtime = Runtime()
        return self._runtime

    def acquire(
        self, *, model: str | None, operation: str, application: str | None = None,
        tool: str | None = None, cancelled: Event | None = None,
    ) -> _Request:
        """Reserve starts and concurrency atomically; wait outside the transaction."""
        application, tool = caller_labels(application, tool)
        pid, process_started, process_instance = identity()
        request_id = str(uuid.uuid4())
        store = self.runtime._store
        request = _Request(store, request_id)
        inserted = False
        try:
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise ConnectionClosedError("Connection closed while waiting for its request slot.")
                now, tick = time.time(), time.monotonic()
                admitted = False
                with store.transaction() as db:
                    _maintain(db, now, tick)
                    bucket = db.execute("SELECT fingerprint FROM buckets WHERE id=?", (self.bucket,)).fetchone()
                    if bucket is not None and bucket[0] != self.fingerprint:
                        busy = db.execute(
                            "SELECT 1 FROM requests WHERE bucket=? AND state IN (?, ?, ?) LIMIT 1",
                            (self.bucket, *_ACTIVE),
                        ).fetchone()
                        recent = db.execute("SELECT 1 FROM starts WHERE bucket=? LIMIT 1", (self.bucket,)).fetchone()
                        if busy or recent:
                            raise CoordinationError(
                                "Conflicting source definitions share a runtime bucket. Stop/reload "
                                "old clients and let the 60-second request window expire."
                            )
                    db.execute("INSERT OR REPLACE INTO buckets VALUES (?, ?)", (self.bucket, self.fingerprint))
                    if not inserted:
                        db.execute(
                            "INSERT INTO requests (request_id, bucket, registry_id, source, model, operation, "
                            "pid, process_started, process_instance, application, tool, state, created_at, created_tick) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?)",
                            (request_id, self.bucket, self.registry_id, self.name, model, operation,
                             pid, process_started, process_instance, application, tool, now, tick),
                        )
                    elif db.execute("SELECT 1 FROM requests WHERE request_id=? AND state='waiting'", (request_id,)).fetchone() is None:
                        raise CoordinationError("Waiting request record was lost; refusing to reset its budget.")
                    active = db.execute(
                        "SELECT count(*) FROM requests WHERE bucket=? AND state IN ('running', 'streaming')",
                        (self.bucket,),
                    ).fetchone()[0]
                    count = db.execute("SELECT count(*) FROM starts WHERE bucket=?", (self.bucket,)).fetchone()[0]
                    concurrent = self.limits.get("max_concurrent")
                    rpm = self.limits.get("requests_per_minute")
                    if (concurrent is None or active < concurrent) and (rpm is None or count < rpm):
                        if cancelled is not None and cancelled.is_set():
                            raise ConnectionClosedError("Connection closed before request admission.")
                        db.execute("INSERT INTO starts VALUES (?, ?)", (self.bucket, tick))
                        db.execute(
                            "UPDATE requests SET state='running', started_at=?, started_tick=? WHERE request_id=?",
                            (now, tick, request_id),
                        )
                        admitted = True
                inserted = True  # Only after commit, so rollback needs no invented cleanup.
                if admitted:
                    return request
                if cancelled is None:
                    time.sleep(_POLL_SECONDS)
                else:
                    cancelled.wait(_POLL_SECONDS)
        except BaseException as exc:
            if inserted:
                try:
                    request.finish("cancelled" if isinstance(exc, (KeyboardInterrupt, ConnectionClosedError)) else "error")
                except CoordinationError as cleanup:
                    exc.add_note(str(cleanup))
            raise
