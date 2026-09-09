"""The narrow integration boundary with LLM's OpenAI-compatible Chat model.

LLM owns prompting, conversations, schemas, tools, response objects, and streaming.
Sheetbend only supplies the client, source-local limits, and connection lifetime.
The optional dependency range is deliberately bounded because Chat is an upstream
implementation class rather than a provider-independent constructor.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from typing import Any

import os

import llm
from llm.default_plugins.openai_models import Chat
import openai

from .errors import ConnectionClosedError, CredentialError
from .rate_limiter import RateLimiter, _Permit
from .source import Source


class _ConnectedChat(Chat):
    """An LLM model with a source-bound client and explicit resource lifetime."""

    def __init__(
        self, *, client: openai.OpenAI, limiter: RateLimiter, **options: Any
    ) -> None:
        super().__init__(**options)
        self._client = client
        self._limiter = limiter
        self._closed = False
        self._lock = Lock()
        self._permits: set[_Permit] = set()
        # Prevent LLM's KeyModel from consulting its own key store/environment.
        self.needs_key = None
        self.key_env_var = None

    def get_key(self, explicit_key: str | None = None) -> str:
        if explicit_key is not None:
            raise ValueError("Use the source's auth configuration, not prompt(key=...).")
        return "sheetbend-source-bound"

    def get_client(self, key: str | None, *, async_: bool = False) -> openai.OpenAI:
        if async_:
            raise NotImplementedError("Sheetbend 0.1 provides synchronous LLM connections only.")
        self._ensure_open()
        return self._client

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConnectionClosedError("Consume this response inside source.connect().")

    def execute(
        self,
        prompt: llm.Prompt,
        stream: bool,
        response: llm.Response,
        conversation: llm.Conversation | None = None,
        key: str | None = None,
    ) -> Iterator[Any]:
        self._ensure_open()
        permit = self._limiter.acquire()
        with self._lock:
            if self._closed:
                permit.release()
                self._ensure_open()
            self._permits.add(permit)
        execution = super().execute(prompt, stream, response, conversation, key)
        try:
            while True:
                self._ensure_open()
                try:
                    chunk = next(execution)
                except StopIteration:
                    break
                self._ensure_open()
                yield chunk
        finally:
            try:
                execution.close()
            finally:
                permit.release()
                with self._lock:
                    self._permits.discard(permit)

    def _close(self) -> None:
        # Release abandoned streaming permits as well as normally consumed ones.
        # The surrounding OpenAI context closes the actual HTTP resources.
        with self._lock:
            self._closed = True
            for permit in self._permits:
                permit.release()
            self._permits.clear()

    def __repr__(self) -> str:
        return f"SheetbendModel(model={self.model_id!r}, closed={self._closed})"


@contextmanager
def connected_model(
    source: Source, *, model: str, probe_capability: str | None = None
) -> Iterator[llm.KeyModel]:
    """Bind an LLM model without registering aliases or writing LLM configuration."""
    if os.environ.get("OPENAI_CUSTOM_HEADERS"):
        raise CredentialError("Unset OPENAI_CUSTOM_HEADERS before opening a source-bound connection.")
    definition = source.to_dict()
    capabilities = definition["capabilities"]
    if probe_capability is not None:
        capabilities[probe_capability] = True
    headers: dict[str, Any] = source.resolve_auth()
    # Even no-auth/custom-header clients need an SDK placeholder key. Explicitly
    # omit its Authorization header; never borrow OPENAI_API_KEY or LLM keys.
    headers.setdefault("Authorization", openai.Omit())
    with openai.DefaultHttpxClient(
        verify=source._tls_context(),
        trust_env=False,
        follow_redirects=False,
    ) as http_client, openai.OpenAI(
        base_url=source.base_url,
        api_key="sheetbend-source-bound",
        admin_api_key="",
        webhook_secret="",
        organization="",
        project="",
        default_headers=headers,
        timeout=definition["timeout_seconds"],
        max_retries=0,
        http_client=http_client,
    ) as client:
        connected = _ConnectedChat(
            client=client,
            limiter=source._limiter,
            model_id=model,
            model_name=model,
            api_base=source.base_url,
            can_stream=capabilities["streaming"],
            supports_schema=capabilities["json_schema"],
            vision=capabilities["vision"],
            supports_tools=capabilities["tools"],
            allows_system_prompt=capabilities["system_prompt"],
        )
        try:
            yield connected
        finally:
            connected._close()
