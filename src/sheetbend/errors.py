"""Errors that callers can handle without parsing human-readable messages."""


class SheetbendError(Exception):
    """Base exception for Sheetbend configuration and connection failures."""


class ConfigError(SheetbendError):
    """The selected configuration is unreadable or violates its contract."""


class SelectionError(SheetbendError):
    """No explicitly permitted source can satisfy the requested selection."""


class CredentialError(SheetbendError):
    """A configured credential reference could not be resolved safely."""


class DependencyError(SheetbendError):
    """An explicitly requested integration is not installed."""


class ConnectionClosedError(SheetbendError):
    """An unfinished operation outlived its connection context."""


class ProbeError(SheetbendError):
    """A diagnostic response did not satisfy the probe's expected contract."""


class CoordinationError(SheetbendError):
    """Host-local request coordination failed; no unthrottled fallback is allowed."""
