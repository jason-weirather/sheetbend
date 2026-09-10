"""Single-file discovery and the authoritative packaged configuration contract."""

from collections.abc import Mapping
from copy import deepcopy
from importlib.resources import files
import ipaddress
import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker

from .errors import ConfigError

PathLike = str | os.PathLike[str]


def load_schema(name: str = "config") -> dict[str, Any]:
    """Return an independent copy of a packaged schema: config, check, check-report, or activity."""
    if name not in {"config", "check", "check-report", "activity"}:
        raise ValueError("Unknown packaged schema name.")
    resource = files("sheetbend").joinpath("schemas", f"{name}.schema.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def select_config_path(config_path: PathLike | None = None) -> tuple[Path, bool]:
    """Return the selected path and whether its absence is an error."""
    if config_path is not None:
        return Path(config_path).expanduser().absolute(), True
    selected = os.environ.get("SHEETBEND_CONFIG")
    if selected:
        return Path(selected).expanduser().absolute(), True
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    # XDG explicitly requires ignoring non-absolute base-directory values.
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / "sheetbend" / "config.toml", False


def _apply_defaults(value: Any, node: Mapping[str, Any], root: Mapping[str, Any]) -> None:
    """Materialize this schema's local object defaults without changing caller data."""
    if "$ref" in node:
        node = root["$defs"][node["$ref"].rsplit("/", 1)[-1]]
    if not isinstance(value, dict):
        return
    properties = node.get("properties", {})
    for name, child in properties.items():
        if name not in value and "default" in child:
            value[name] = deepcopy(child["default"])
        if name in value:
            _apply_defaults(value[name], child, root)
    additional = node.get("additionalProperties")
    if isinstance(additional, dict):
        for name in value.keys() - properties.keys():
            _apply_defaults(value[name], additional, root)


def _require_json(value: Any) -> None:
    """Reject TOML dates/non-finite numbers before JSON Schema sees the value."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ConfigError("Configuration must contain JSON-compatible, finite values.") from exc


def _validate_endpoint(name: str, source: Mapping[str, Any]) -> None:
    try:
        url = urlsplit(source["base_url"])
        host = url.hostname
        port = url.port
        valid = host and url.scheme in {"http", "https"} and not (
            url.username is not None or url.password is not None or url.query or url.fragment
        )
        if not valid or (port is not None and port == 0):
            raise ValueError("Invalid endpoint")
    except ValueError as exc:
        raise ConfigError(f"Source {name!r}: invalid base_url.") from exc
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if url.scheme == "http" and not loopback and not source["allow_insecure_http"]:
        raise ConfigError(
            f"Source {name!r}: non-loopback HTTP requires allow_insecure_http = true."
        )
    auth = source["auth"]
    if auth["type"] == "header":
        forbidden = {
            "authorization", "proxy-authorization", "host", "content-length", "content-type",
            "transfer-encoding", "connection", "upgrade", "trailer", "te", "cookie",
        }
        if auth["header"].lower() in forbidden:
            raise ConfigError(f"Source {name!r}: auth.header is reserved; use a credential header.")
    if not math.isfinite(source["timeout_seconds"]):
        raise ConfigError(f"Source {name!r}: timeout_seconds must be finite.")


def validate_config(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy parsed config; apply defaults authored only in JSON Schema.

    The input is not changed. Cross-reference and transport rules supplement the
    schema where ordinary JSON Schema cannot express them. Secrets are not resolved.
    """
    owned = deepcopy(dict(data))
    _require_json(owned)
    schema = load_schema()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    error = next(validator.iter_errors(owned), None)
    if error is not None:
        path = ".".join(map(str, error.absolute_path)) or "<root>"
        # Report schema-owned expectations, not rejected literal values (secrets).
        detail = f"violates {error.validator}"
        if error.validator == "required":
            missing = [key for key in error.validator_value if key not in error.instance]
            detail = f"missing required field(s): {', '.join(missing)}"
        elif error.validator in {"type", "enum", "const", "minimum", "exclusiveMinimum"}:
            detail = f"expected {error.validator} {error.validator_value!r}"
        elif error.validator == "additionalProperties":
            detail = "unknown field(s); consult the packaged config schema"
        raise ConfigError(f"Invalid configuration at {path}: {detail}.")
    _apply_defaults(owned, schema, schema)
    default = owned.get("default_source")
    if default is not None and default not in owned["sources"]:
        raise ConfigError(f"default_source {default!r} is not defined in sources.")
    for name, source in owned["sources"].items():
        _validate_endpoint(name, source)
        if source["default_model"] not in source["models"]:
            raise ConfigError(f"Source {name!r}: default_model must name a configured model.")
    return owned
