"""The selected, validated registry and explicit source selection."""

from collections.abc import Collection, Mapping
from copy import deepcopy
from pathlib import Path
import tomllib
from typing import Any

from .config import PathLike, select_config_path, validate_config
from .errors import ConfigError, SelectionError
from .source import Source


class Registry:
    """An owned configuration snapshot; use from_file() or from_dict().

    Reading and displaying a registry never resolve secrets or access endpoints.
    Repeated source() calls return the same Source, sharing its in-process limits.
    """

    def __init__(self, data: Mapping[str, Any], *, config_path: Path | None = None) -> None:
        self._data = validate_config(data)
        self.config_path = config_path
        base_dir = config_path.parent if config_path is not None else Path.cwd()
        self._sources = {
            name: Source(name, definition, base_dir=base_dir)
            for name, definition in self._data["sources"].items()
        }

    @classmethod
    def from_file(cls, config_path: PathLike | None = None) -> "Registry":
        """Read exactly one TOML file. Only a missing implicit default is empty."""
        path, explicit = select_config_path(config_path)
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except FileNotFoundError as exc:
            # A broken symlink is an invalid configured file, not a missing default.
            if explicit or path.is_symlink():
                raise ConfigError(f"Configuration file does not exist: {path}") from exc
            data = {"schema_version": 1, "sources": {}}
        except (OSError, tomllib.TOMLDecodeError, UnicodeError) as exc:
            raise ConfigError(f"Cannot read valid TOML configuration: {path}") from exc
        return cls(data, config_path=path)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Registry":
        """Create an independent registry from parsed configuration in a notebook."""
        return cls(data)

    @property
    def source_names(self) -> tuple[str, ...]:
        """Names in configuration order, without resolving credentials."""
        return tuple(self._sources)

    @property
    def default_source(self) -> str | None:
        return self._data.get("default_source")

    def source(
        self,
        name: str | None = None,
        *,
        allowed_scopes: Collection[str] | None = None,
        organization: str | None = None,
    ) -> Source:
        """Select one source; never guess, reroute, or relax explicit restrictions.

        organization requires that exact institutional boundary. Scopes describe
        configured processing boundaries, not verified data-handling permissions.
        """
        if isinstance(allowed_scopes, str):
            raise TypeError("allowed_scopes must be a collection of scope names, not a string.")
        if allowed_scopes is not None:
            unknown = set(allowed_scopes) - {"local", "institutional", "external"}
            if unknown:
                raise ValueError(f"Unknown allowed scopes: {sorted(unknown)}")
        selected = self.default_source if name is None else name
        if selected is None:
            raise SelectionError("Select a source by name or configure default_source.")
        try:
            source = self._sources[selected]
        except KeyError as exc:
            raise SelectionError(f"Source {selected!r} is not configured.") from exc
        if allowed_scopes is not None and source.scope not in allowed_scopes:
            raise SelectionError(f"Source {selected!r} is outside allowed_scopes.")
        if organization is not None and (
            source.scope != "institutional" or source.organization != organization
        ):
            raise SelectionError(f"Source {selected!r} is outside the requested organization.")
        return source

    def to_dict(self) -> dict[str, Any]:
        """Return an independent, normalized configuration; never include secrets."""
        return deepcopy(self._data)

    def __repr__(self) -> str:
        return (
            f"Registry(sources={list(self._sources)!r}, "
            f"default_source={self.default_source!r}, config_path={str(self.config_path)!r})"
        )
