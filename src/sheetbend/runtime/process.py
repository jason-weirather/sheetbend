"""Process identity and liveness without reading argv, files, or environment values."""

import hashlib
import os
from pathlib import Path
import sys

import psutil

from ..errors import CoordinationError


def identity() -> tuple[int, float, str]:
    try:
        process = psutil.Process(os.getpid())
        started = process.create_time()
    except psutil.Error as exc:
        raise CoordinationError("Cannot establish request process identity.") from exc
    instance = hashlib.sha256(f"{process.pid}:{started:.6f}".encode()).hexdigest()[:24]
    return process.pid, started, instance


def alive(pid: int, started: float) -> bool:
    try:
        process = psutil.Process(pid)
        return process.create_time() == started and process.status() not in (
            psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD,
        )
    except psutil.NoSuchProcess:
        return False
    except psutil.Error as exc:
        # Uncertainty does not grant permission to evict somebody else's slot.
        raise CoordinationError("Cannot establish a recorded process's liveness.") from exc


def caller_labels(application: str | None, tool: str | None) -> tuple[str, str | None]:
    """Display metadata only. Never derive a label from the command line."""
    if application is None:
        application = Path(sys.executable).name or "python"
    for name, value in (("application", application), ("tool", tool)):
        if value is not None and (not isinstance(value, str) or not value.strip()
                                  or len(value) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError(f"{name} must be a printable, nonempty label of at most 80 characters.")
    return application, tool
