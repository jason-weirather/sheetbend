"""Lazy credential references. No secret database, shell evaluation, or implicit fallback."""

from collections.abc import Mapping
from copy import deepcopy
import os
from pathlib import Path
import stat
import subprocess
import sys
import tomllib
from typing import Any

from .errors import CredentialError

_MAX_FILE_BYTES = 65536


def header_secret(value: Any) -> str:
    """Validate without including rejected contents in an exception."""
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in value)):
        raise CredentialError("Credential is empty or not a safe HTTP header value.")
    return value


def _read_private_file(path: Path) -> bytes:
    """Check the opened descriptor, not a racy pre-open stat. Never block on a FIFO."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > _MAX_FILE_BYTES):
            raise CredentialError("Credential file must be an owner-only, single-link regular file (0600).")
        with os.fdopen(fd, 'rb') as handle:
            fd = None
            data = handle.read(_MAX_FILE_BYTES + 1)
        if len(data) > _MAX_FILE_BYTES:
            raise CredentialError("Credential file exceeds the size limit.")
        return data
    except OSError:
        raise CredentialError("Credential file is unavailable or unsafe; symlinks are not allowed.") from None
    finally:
        if fd is not None:
            os.close(fd)


class CredentialStore:
    """An owned, unresolved set of references, relative to the selected config file."""

    def __init__(self, definitions: Mapping[str, Any], base_dir: Path) -> None:
        self._definitions = deepcopy(dict(definitions))
        self._base_dir = base_dir

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    def definition(self, name: str) -> dict[str, Any]:
        try:
            return deepcopy(self._definitions[name])
        except KeyError:
            raise CredentialError("Credential reference is not configured.") from None

    def resolve(self, name: str) -> str:
        """Explicit escape hatch: the returned string is a secret. Never log it."""
        spec = self.definition(name)
        provider = spec['provider']
        if provider == 'env':
            value = os.environ.get(spec['name'])
        elif provider == 'file':
            path = Path(spec['path']).expanduser()
            if not path.is_absolute():
                path = self._base_dir / path
            try:
                text = _read_private_file(path).decode('utf-8')
                if 'key' in spec:
                    value = tomllib.loads(text).get(spec['key'])
                else:
                    # One optional final line ending, not arbitrary whitespace stripping.
                    value = text.removesuffix('\n').removesuffix('\r')
            except (UnicodeError, tomllib.TOMLDecodeError):
                raise CredentialError("Credential file is not valid UTF-8/TOML.") from None
        elif provider == 'keychain':
            if sys.platform != 'darwin':
                raise CredentialError("The keychain provider requires macOS.")
            try:
                result = subprocess.run(
                    ['/usr/bin/security', 'find-generic-password', '-s', spec['service'],
                     '-a', spec['account'], '-w'],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False,
                )
                if result.returncode:
                    raise CredentialError("Keychain credential is unavailable or access was denied.")
                value = result.stdout.decode('utf-8').removesuffix('\n').removesuffix('\r')
            except (OSError, subprocess.TimeoutExpired, UnicodeError):
                raise CredentialError("Keychain lookup failed or timed out.") from None
        else:
            raise CredentialError("Unsupported credential provider.")
        return header_secret(value)

    def __repr__(self) -> str:
        return f'CredentialStore(references={len(self._definitions)}, resolved=False)'
