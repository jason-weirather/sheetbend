"""The selected, validated registry and deterministic source resolution."""

from collections.abc import Collection, Mapping
from copy import deepcopy
from pathlib import Path
import uuid
import tomllib
from typing import Any

from .config import PathLike, select_config_path, validate_config
from .errors import ConfigError, SelectionError
from .source import Source


class Registry:
    """An owned configuration snapshot; use from_file() or from_dict().

    Reading and displaying a registry never resolve secrets or access endpoints.
    Repeated source() calls return the same Source. File-backed registries
    share host-local limits across cooperating applications.
    """

    def __init__(self, data: Mapping[str, Any], *, config_path: Path | None = None) -> None:
        self._data = validate_config(data)
        self.config_path = Path(config_path).expanduser().absolute() if config_path is not None else None
        namespace = str(self.config_path.resolve()) if self.config_path is not None else f"memory:{uuid.uuid4()}"
        base_dir = self.config_path.parent if self.config_path is not None else Path.cwd()
        self._sources = {
            name: Source(name, definition, base_dir=base_dir, namespace=namespace)
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
            data = {"schema_version": 2, "sources": {}}
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
        allowed_scopes: Collection[str] = ("local", "institutional"),
        organization: str | None = None,
    ) -> Source:
        """Select one source using explicit names or ordered scope constraints.

        An explicit name is absolute and must satisfy the supplied constraints.
        Without a name, ``allowed_scopes`` is both an allow-list and a priority
        order. The first scope with eligible sources wins. The configured default
        breaks ties only inside that highest-priority eligible scope; otherwise a
        unique source is selected and ambiguity fails. External access must still
        be explicitly included. ``organization`` narrows institutional candidates.
        """
        if allowed_scopes is None or isinstance(allowed_scopes, str):
            raise TypeError("allowed_scopes must be a collection of scope names, not a string.")
        if isinstance(allowed_scopes, (set, frozenset)) and len(allowed_scopes) > 1:
            raise TypeError("allowed_scopes with multiple entries must preserve priority order.")
        scope_order = tuple(allowed_scopes)
        if len(set(scope_order)) != len(scope_order):
            raise ValueError("allowed_scopes must not contain duplicate scope names.")
        unknown = set(scope_order) - {"local", "institutional", "external"}
        if unknown:
            raise ValueError(f"Unknown allowed scopes: {sorted(unknown)}")

        def eligible(source: Source) -> bool:
            return (
                source.scope in scope_order
                and (
                    organization is None
                    or (source.scope == "institutional" and source.organization == organization)
                )
            )

        if name is not None:
            try:
                source = self._sources[name]
            except KeyError as exc:
                raise SelectionError(f"Source {name!r} is not configured.") from exc
            if source.scope not in scope_order:
                raise SelectionError(f"Source {name!r} is outside allowed_scopes.")
            if organization is not None and not eligible(source):
                raise SelectionError(f"Source {name!r} is outside the requested organization.")
            return source

        default = self._sources.get(self.default_source)
        for scope in scope_order:
            candidates = [
                source for source in self._sources.values()
                if source.scope == scope and eligible(source)
            ]
            if not candidates:
                continue
            if default in candidates:
                return default
            if len(candidates) == 1:
                return candidates[0]
            names = ", ".join(source.name for source in candidates)
            raise SelectionError(
                f"Multiple sources satisfy highest-priority scope {scope!r}: {names}. "
                "Select one explicitly."
            )

        qualifier = f" and organization {organization!r}" if organization is not None else ""
        raise SelectionError(
            f"No configured source satisfies allowed_scopes {scope_order!r}{qualifier}."
        )

    def to_dict(self) -> dict[str, Any]:
        """Return an independent, normalized configuration; never include secrets."""
        return deepcopy(self._data)

    def __repr__(self) -> str:
        return (
            f"Registry(sources={list(self._sources)!r}, "
            f"default_source={self.default_source!r}, config_path={str(self.config_path)!r})"
        )
