"""Private, boot-scoped SQLite storage on a host-local filesystem."""

from contextlib import contextmanager
from collections.abc import Iterator
import hashlib
import os
import re
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import psutil

from ..errors import CoordinationError

_REMOTE = {"nfs", "nfs4", "cifs", "smbfs", "sshfs", "fuse.sshfs", "9p", "afs", "lustre", "ceph", "glusterfs"}
_DDL = (
    "CREATE TABLE IF NOT EXISTS buckets (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS starts (bucket TEXT NOT NULL, tick REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS starts_bucket ON starts(bucket, tick)",
    """CREATE TABLE IF NOT EXISTS requests (
        request_id TEXT PRIMARY KEY, bucket TEXT NOT NULL, registry_id TEXT NOT NULL,
        source TEXT NOT NULL, model TEXT, operation TEXT NOT NULL,
        pid INTEGER NOT NULL, process_started REAL NOT NULL, process_instance TEXT NOT NULL,
        application TEXT NOT NULL, tool TEXT, state TEXT NOT NULL,
        created_at REAL NOT NULL, created_tick REAL NOT NULL,
        started_at REAL, started_tick REAL, finished_at REAL, finished_tick REAL,
        input_tokens INTEGER, output_tokens INTEGER)""",
    "CREATE INDEX IF NOT EXISTS requests_bucket ON requests(bucket, state)",
)


def boot_id() -> str:
    """Use an OS boot identifier, not wall-clock estimates that can change."""
    try:
        if sys.platform.startswith("linux"):
            value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        elif sys.platform == "darwin":
            result = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
                check=True, capture_output=True, text=True, timeout=5,
            )
            value = result.stdout.strip()
        else:
            raise CoordinationError("Shared runtime currently supports Linux and macOS only.")
    except (OSError, subprocess.SubprocessError) as exc:
        raise CoordinationError("Cannot determine this host's boot identity.") from exc
    if not value:
        raise CoordinationError("The OS returned an empty boot identity.")
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def runtime_directory(directory: str | os.PathLike[str] | None = None) -> Path:
    """Select a directory without creating it. Never use TMPDIR or the home fallback."""
    selected = directory if directory is not None else os.environ.get("SHEETBEND_RUNTIME_DIR")
    if selected is not None:
        path = Path(selected).expanduser()
        if not path.is_absolute():
            raise CoordinationError("SHEETBEND_RUNTIME_DIR/runtime directory must be absolute.")
        return path
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    if xdg and Path(xdg).is_absolute():
        return Path(xdg) / "sheetbend"
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
                check=True, capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CoordinationError("Cannot determine the macOS user runtime directory.") from exc
        local_temp = result.stdout.strip()
        if not local_temp:
            raise CoordinationError("macOS returned an empty user runtime directory.")
        return Path(local_temp) / "sheetbend"
    if sys.platform.startswith("linux"):
        return Path("/tmp") / f"sheetbend-{os.getuid()}"
    raise CoordinationError("Shared runtime currently supports Linux and macOS only.")


def _private_directory(path: Path, *, create: bool) -> bool:
    try:
        if create:
            # Parents must already exist. Do not create a potentially shared tree.
            path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CoordinationError("Runtime directory must be a user-owned, non-symlink 0700 directory.")
    resolved = path.resolve()
    partitions = [p for p in psutil.disk_partitions(all=True)
                  if resolved.is_relative_to(Path(p.mountpoint).resolve())]
    if partitions:
        partition = max(partitions, key=lambda p: len(p.mountpoint))
        if partition.fstype.lower() in _REMOTE:
            raise CoordinationError("Runtime coordination must not use a network filesystem.")
    return True


def _private_file(path: Path, *, create: bool = False) -> None:
    if create:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if create:
            raise CoordinationError("The runtime database disappeared during creation.")
        return  # SQLite may remove a WAL/SHM sidecar between concurrent operations.
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise CoordinationError("Runtime files must be private, user-owned regular files.")


class Store:
    """A path, not a persistent SQLite connection; safe to use from multiple threads."""

    def __init__(self, directory: str | os.PathLike[str] | None = None) -> None:
        self.directory = runtime_directory(directory)
        self._path: Path | None = None

    @property
    def path(self) -> Path:
        if self._path is None:
            self._path = self.directory / f"runtime-{boot_id()}.sqlite3"
        return self._path

    @contextmanager
    def transaction(self, *, create: bool = True) -> Iterator[sqlite3.Connection | None]:
        """Short atomic admission/update transaction; no network work belongs here."""
        connection = None
        try:
            if not _private_directory(self.directory, create=create):
                if create:
                    raise CoordinationError("Runtime directory parent does not exist.")
                yield None
                return
            if not create and not self.path.exists() and not self.path.is_symlink():
                yield None
                return
            if create:
                # Old boot identities cannot have living callers on this host.
                for previous in self.directory.iterdir():
                    if (re.fullmatch(r"runtime-[0-9a-f]{24}\.sqlite3(?:-(?:wal|shm|journal))?", previous.name)
                            and not previous.name.startswith(self.path.name)):
                        _private_file(previous)
                        previous.unlink(missing_ok=True)
            _private_file(self.path, create=create)
            for suffix in ("-wal", "-shm", "-journal"):
                _private_file(Path(str(self.path) + suffix))
            connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise CoordinationError("Unsupported runtime database version; stop old clients first.")
            for statement in _DDL:
                connection.execute(statement)
            connection.execute("PRAGMA user_version=1")
            yield connection
            connection.commit()
        except (OSError, sqlite3.Error, psutil.Error) as exc:
            raise CoordinationError("Host-local runtime storage failed; no unlimited fallback.") from exc
        finally:
            if connection is not None:
                connection.close()
