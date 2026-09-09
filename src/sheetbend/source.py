"""A named intelligence endpoint, its declarations, and its useful workflow stages."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import ssl
from typing import TYPE_CHECKING, Any

import httpx

from .errors import CredentialError, DependencyError, ProbeError
from .rate_limiter import RateLimiter

if TYPE_CHECKING:
    import llm
    from .check_result import CheckResult


class Source:
    """A registry-owned source. Obtain instances with Registry.source().

    Definitions have already passed the registry's validation barrier. Source
    owns no persistent network resources; connect() gives those an explicit scope.
    """

    def __init__(self, name: str, definition: Mapping[str, Any], *, base_dir: Path) -> None:
        self._name = name
        self._definition = deepcopy(dict(definition))
        self._base_dir = base_dir
        self._limiter = RateLimiter(**definition["rate_limit"])

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
        """Resolve the configured auth to HTTP headers; returned values ARE secrets.

        Nothing is cached, printed, written, or looked up in another client's key
        store. Callers requesting this explicit escape hatch own its disclosure.
        connect() resolves once per context, so reconnect after rotating a secret.
        """
        auth = self._definition["auth"]
        if auth["type"] == "none":
            return {}
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

    def _selected_model(self, model: str | None) -> str:
        selected = self.default_model if model is None else model
        if not isinstance(selected, str) or not selected.strip():
            raise ValueError("model must be a nonempty model identifier.")
        return selected

    def list_models(self) -> list[str]:
        """Explicitly GET the endpoint's /models catalog, without generating text.

        A catalog is not proof that a particular model can run. No pagination,
        cross-origin links, redirects, retries, or inferred alternative paths.
        """
        headers = self.resolve_auth()
        with httpx.Client(
            headers=headers,
            timeout=self._definition["timeout_seconds"],
            verify=self._tls_context(),
            trust_env=False,
            follow_redirects=False,
        ) as client, self._limiter.acquire():
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
    def connect(self, model: str | None = None) -> Iterator["llm.KeyModel"]:
        """Yield a synchronous LLM model scoped to this connection context.

        This is preparation, not a health check. Requests, lazy responses, and
        streaming must be consumed inside the context. Completed response data
        can be retained. The model cannot start requests after the context exits.
        The source's capability declarations must match any overridden model.
        """
        with self._connect(self._selected_model(model)) as connected:
            yield connected

    @contextmanager
    def _connect(
        self, model: str, *, probe_capability: str | None = None
    ) -> Iterator["llm.KeyModel"]:
        try:
            from .llm_adapter import connected_model
        except ImportError as exc:
            raise DependencyError("LLM integration requires: pip install 'sheetbend[llm]'") from exc
        with connected_model(self, model=model, probe_capability=probe_capability) as connected:
            yield connected

    def check(self, *, test: str = "models", model: str | None = None) -> "CheckResult":
        """Run one explicit diagnostic; text/schema/stream tests send synthetic prompts.

        A failed probe is returned as a structured result, unlike ordinary source
        operations, which raise. This never edits capability declarations or sends
        user files, notebook variables, or prompt history.
        """
        from .checks import check_source

        return check_source(self, test=test, model=self._selected_model(model))

    def __repr__(self) -> str:
        return f"Source(name={self.name!r}, scope={self.scope!r}, model={self.default_model!r})"
