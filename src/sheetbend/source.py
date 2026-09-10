"""A named intelligence endpoint, its declarations, and its useful workflow stages."""

from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import ssl
from typing import TYPE_CHECKING, Any

import httpx

from .errors import CredentialError, DependencyError, ProbeError, SelectionError
from .config import load_schema, _apply_defaults
from .runtime.runtime import _Admission
from .runtime.process import caller_labels

if TYPE_CHECKING:
    import llm
    from .check_result import CheckResult, CheckReport


class Source:
    """A registry-owned source. Obtain instances with Registry.source().

    Definitions have already passed the registry's validation barrier. Source
    owns no persistent network resources; connect() gives those an explicit scope.
    """

    def __init__(
        self, name: str, definition: Mapping[str, Any], *, base_dir: Path, namespace: str
    ) -> None:
        self._name = name
        self._definition = deepcopy(dict(definition))
        self._base_dir = base_dir
        self._admission = _Admission(namespace, name, definition)

    @property
    def name(self) -> str:
        return self._name

    @property
    def scope(self) -> str:
        return self._definition["scope"]

    @property
    def organization(self) -> str | None:
        return self._definition.get("organization")

    @property
    def default_model(self) -> str:
        return self._definition["default_model"]

    @property
    def base_url(self) -> str:
        return self._definition["base_url"]

    def to_dict(self) -> dict[str, Any]:
        """Copy the source's normalized definition, not resolved credentials."""
        return deepcopy(self._definition)

    def resolve_auth(self) -> dict[str, str]:
        """Resolve configured auth to HTTP headers; returned values may be secrets.

        Nothing is cached, printed, written, or looked up in another client's key
        store. ``bearer-placeholder`` returns the fixed non-secret value
        ``Authorization: Bearer sheetbend`` for endpoints that require bearer-shaped
        authentication without validating a credential. Reconnect after rotating a
        real secret.
        """
        auth = self._definition["auth"]
        if auth["type"] == "none":
            return {}
        if auth["type"] == "bearer-placeholder":
            return {"Authorization": "Bearer sheetbend"}
        env = auth["env"]
        secret = os.environ.get(env)
        if not secret or not secret.strip():
            raise CredentialError(f"Source {self.name!r}: set environment variable {env}.")
        if secret != secret.strip() or any(ord(c) < 32 or ord(c) > 126 for c in secret):
            raise CredentialError(f"Source {self.name!r}: {env} is not a safe HTTP header value.")
        if auth["type"] == "bearer":
            return {"Authorization": f"Bearer {secret}"}
        return {auth["header"]: secret}

    def _tls_context(self) -> ssl.SSLContext:
        bundle = self._definition.get("ca_bundle")
        if bundle is None:
            return ssl.create_default_context()
        path = Path(bundle).expanduser()
        if not path.is_absolute():
            path = self._base_dir / path
        try:
            return ssl.create_default_context(cafile=str(path))
        except (OSError, ssl.SSLError) as exc:
            raise CredentialError(f"Source {self.name!r}: cannot load configured CA bundle.") from exc

    @property
    def model_names(self) -> tuple[str, ...]:
        """Configured server model IDs, not a remotely discovered catalog."""
        return tuple(self._definition["models"])

    def _selected_model(self, model: str | None, *, probe: bool = False) -> str:
        selected = self.default_model if model is None else model
        if (not isinstance(selected, str) or not selected.strip()
                or any(ord(c) < 32 or ord(c) == 127 for c in selected)):
            raise ValueError("model must be a printable, nonempty model identifier.")
        if not probe and selected not in self._definition["models"]:
            raise SelectionError(f"Model {selected!r} is not configured for source {self.name!r}.")
        return selected

    def _model_definition(self, model: str, *, probe: bool = False) -> dict[str, Any]:
        selected = self._selected_model(model, probe=probe)
        if selected in self._definition["models"]:
            return deepcopy(self._definition["models"][selected])
        # An explicit probe may try an unconfigured ID, using schema defaults,
        # never another model's declarations.
        definition: dict[str, Any] = {}
        schema = load_schema()
        _apply_defaults(definition, schema["$defs"]["model"], schema)
        return definition

    def list_models(
        self, *, application: str | None = None, tool: str | None = None
    ) -> list[str]:
        """Explicitly GET the endpoint's /models catalog, without generating text.

        A catalog is not proof that a particular model can run. No pagination,
        cross-origin links, redirects, retries, or inferred alternative paths.
        """
        application, tool = caller_labels(application, tool)
        headers = self.resolve_auth()
        with httpx.Client(
            headers=headers,
            timeout=self._definition["timeout_seconds"],
            verify=self._tls_context(),
            trust_env=False,
            follow_redirects=False,
        ) as client, self._admission.acquire(
            model=None, operation="models", application=application, tool=tool
        ):
            response = client.get(self.base_url.rstrip("/") + "/models")
            response.raise_for_status()
            try:
                body = response.json()
            except ValueError as exc:
                raise ProbeError("The model catalog is not JSON.") from exc
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ProbeError("The model catalog must contain a data array.")
        models = body["data"]
        if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in models):
            raise ProbeError("Each model-catalog entry must have a string id.")
        return [row["id"] for row in models]

    @contextmanager
    def connect(
        self, model: str | None = None, *, requires: Collection[str] = (),
        application: str | None = None, tool: str | None = None,
    ) -> Iterator["llm.KeyModel"]:
        """Yield an LLM model with explicit lifetime and model capability requirements.

        The model must be configured. Requirements check declarations without
        sending probes; nothing reroutes or falls back. Labels describe the caller
        in host-local telemetry, not model tools or security identities. Consume
        lazy responses inside this context and finish workers before leaving it.
        """
        selected = self._selected_model(model)
        capabilities = self._model_definition(selected)["capabilities"]
        if requires is None or isinstance(requires, str):
            raise TypeError("requires must be a collection of capability names.")
        requirements = set(requires)
        unknown = requirements - capabilities.keys()
        if unknown:
            raise ValueError(f"Unknown capabilities: {sorted(unknown)}")
        missing = {name for name in requirements if not capabilities[name]}
        if missing:
            raise SelectionError(f"Model {selected!r} does not declare: {', '.join(sorted(missing))}.")
        application, tool = caller_labels(application, tool)
        with self._connect(selected, application=application, tool=tool) as connected:
            yield connected

    @contextmanager
    def _connect(
        self, model: str, *, probe_capability: str | None = None, probe: bool = False,
        application: str | None = None, tool: str | None = None,
    ) -> Iterator["llm.KeyModel"]:
        definition = self._model_definition(model, probe=probe)
        capabilities = definition["capabilities"]
        if probe_capability is not None:
            capabilities[probe_capability] = True
        try:
            from .llm_adapter import connected_model
        except ImportError as exc:
            raise DependencyError("LLM integration requires: pip install 'sheetbend[llm]'") from exc
        with connected_model(
            self, model=model, capabilities=capabilities, application=application, tool=tool
        ) as connected:
            yield connected

    def check(self, *, test: str = "models", model: str | None = None) -> "CheckResult":
        """Run one explicit synthetic diagnostic; it never changes declarations.

        Generation probes may incur cost. Explicit model IDs may be unconfigured.
        Tools only validates a proposed harmless call; it does not execute code.
        """
        from .checks import check_source

        return check_source(self, test=test, model=self._selected_model(model, probe=True))

    def check_all(self, *, model: str | None = None) -> "CheckReport":
        """Explicitly run all six synthetic probes and collect a versioned report.

        Unlike ordinary operations this diagnostic intentionally collects failures.
        All capability probes run, including undeclared capabilities. No config edits.
        """
        from .check_result import CheckReport
        from .checks import TESTS

        selected = self._selected_model(model, probe=True)
        return CheckReport(tuple(self.check(test=test, model=selected) for test in TESTS))

    def __repr__(self) -> str:
        return f"Source(name={self.name!r}, scope={self.scope!r}, model={self.default_model!r})"
