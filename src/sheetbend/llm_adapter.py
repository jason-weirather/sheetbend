"""The one LLM-specific integration module; LLM still owns the inference API.

This adapter supplies an explicit HTTP client, request admission/activity, and
resource lifetime. It delegates prompting and response handling to LLM Chat.
The upstream dependency range is intentionally bounded and integration-tested.
"""

from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
import os
from threading import Event, Lock
from typing import Any

import llm
from llm.default_plugins.openai_models import Chat
import openai

from .errors import ConnectionClosedError, CredentialError, SelectionError
from .reasoning import ReasoningPlan
from .runtime.runtime import _Admission, _Request
from .source import Source


class _ConnectedChat(Chat):
    """A source-bound LLM model. Caller labels belong to this connection only."""

    def __init__(
        self, *, client: openai.OpenAI, admission: _Admission,
        application: str | None, tool: str | None, omit_authorization: bool,
        reasoning_plan: ReasoningPlan, reasoning_efforts: Collection[str] = (),
        **options: Any,
    ) -> None:
        super().__init__(**options)
        self._client = client
        self._admission = admission
        self._application = application
        self._tool = tool
        self._omit_authorization = omit_authorization
        self._reasoning_plan = reasoning_plan
        self._reasoning_efforts = frozenset(reasoning_efforts)
        self._closed = Event()
        self._pid = os.getpid()
        self._lock = Lock()
        self._requests: set[_Request] = set()
        self.needs_key = None
        self.key_env_var = None

    def get_key(self, explicit_key: str | None = None) -> str:
        if explicit_key is not None:
            raise ValueError("Use the source's auth configuration, not prompt(key=...).")
        return "sheetbend-source-bound"

    def get_client(self, key: str | None, *, async_: bool = False) -> openai.OpenAI:
        if async_:
            raise NotImplementedError("Sheetbend provides synchronous LLM connections only.")
        self._ensure_open()
        return self._client

    def _ensure_open(self) -> None:
        if self._closed.is_set() or self._pid != os.getpid():
            raise ConnectionClosedError("Consume responses in the creating process's source.connect() context.")

    def _validate_native_reasoning(self, prompt: llm.Prompt) -> None:
        """Native LLM effort is an explicit provider-specific escape hatch, not an override."""
        effort = getattr(getattr(prompt, "options", None), "reasoning_effort", None)
        if effort is None:
            return
        plan = self._reasoning_plan
        if plan.selected != "provider" or plan.control != "reasoning_effort":
            raise SelectionError(
                "Reasoning is bound by this connection. Use source.connect(reasoning=...) "
                "instead of also passing prompt options.reasoning_effort."
            )
        if effort not in self._reasoning_efforts:
            raise SelectionError("The native reasoning_effort is not declared for this model.")

    def build_kwargs(self, prompt: llm.Prompt, stream: bool) -> dict[str, Any]:
        """Keep LLM's inference arguments; add only the resolved reasoning/auth controls."""
        self._validate_native_reasoning(prompt)
        kwargs = super().build_kwargs(prompt, stream)
        plan = self._reasoning_plan
        if plan.parameter == "reasoning_effort":
            kwargs["reasoning_effort"] = plan.value
        elif plan.parameter == "chat_template_kwargs.enable_thinking":
            # This is the OpenAI-compatible request body's template option, not
            # Ollama's native /api/chat think flag. No prompt rewriting is involved.
            extra = dict(kwargs.get("extra_body") or {})
            template = dict(extra.get("chat_template_kwargs") or {})
            if "enable_thinking" in template:
                raise SelectionError("Do not override this connection's reasoning control.")
            template["enable_thinking"] = plan.value
            extra["chat_template_kwargs"] = template
            kwargs["extra_body"] = extra
        if self._omit_authorization:
            # OpenAI 3.x recognizes Omit() for authentication validation only when
            # supplied as a per-request override. Putting it in client defaults is
            # stripped before validation and raises TypeError.
            kwargs["extra_headers"] = {"Authorization": openai.Omit()}
        return kwargs

    def execute(
        self, prompt: llm.Prompt, stream: bool, response: llm.Response,
        conversation: llm.Conversation | None = None, key: str | None = None,
    ) -> Iterator[Any]:
        self._ensure_open()
        self._validate_native_reasoning(prompt)
        request = self._admission.acquire(
            model=self.model_id, operation="generate", application=self._application,
            tool=self._tool, cancelled=self._closed,
        )
        with self._lock:
            if self._closed.is_set():
                request.finish("cancelled")
                self._ensure_open()
            self._requests.add(request)
        execution = super().execute(prompt, stream, response, conversation, key)
        state = "cancelled"
        started_stream = False
        try:
            while True:
                self._ensure_open()
                try:
                    chunk = next(execution)
                except StopIteration:
                    state = "done"
                    break
                self._ensure_open()
                if stream and not started_stream:
                    request.streaming()
                    started_stream = True
                yield chunk
        except Exception:
            state = "error"
            raise
        finally:
            try:
                execution.close()
            finally:
                try:
                    # Do not call usage(): it forces an unfinished lazy response.
                    request.finish(
                        state, input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                    )
                finally:
                    with self._lock:
                        self._requests.discard(request)

    def _close(self) -> None:
        self._closed.set()  # Wake waiting admission loops even when no permit exists.
        errors = []
        with self._lock:
            for request in self._requests:
                try:
                    request.finish("cancelled")
                except Exception as exc:
                    errors.append(exc)
            self._requests.clear()
        if errors:
            raise errors[0]

    def __repr__(self) -> str:
        return (
            f"SheetbendModel(model={self.model_id!r}, reasoning={self._reasoning_plan.selected!r}, "
            f"closed={self._closed.is_set()})"
        )


@contextmanager
def connected_model(
    source: Source, *, model: str, capabilities: Mapping[str, bool],
    reasoning_plan: ReasoningPlan, reasoning_efforts: Collection[str] = (),
    application: str | None = None, tool: str | None = None,
) -> Iterator[llm.KeyModel]:
    """Bind LLM without registering aliases, writing logs, or consulting ambient keys."""
    if os.environ.get("OPENAI_CUSTOM_HEADERS"):
        raise CredentialError("Unset OPENAI_CUSTOM_HEADERS before opening a source-bound connection.")
    headers = source.resolve_auth()
    omit_authorization = "Authorization" not in headers
    with openai.DefaultHttpxClient(
        verify=source._tls_context(), trust_env=False, follow_redirects=False,
    ) as http_client, openai.OpenAI(
        base_url=source.base_url, api_key="sheetbend-source-bound", admin_api_key="",
        webhook_secret="", organization="", project="", default_headers=headers,
        timeout=source.to_dict()["timeout_seconds"], max_retries=0, http_client=http_client,
    ) as client:
        connected = _ConnectedChat(
            client=client, admission=source._admission, application=application, tool=tool,
            omit_authorization=omit_authorization, model_id=model, model_name=model,
            api_base=source.base_url, reasoning_plan=reasoning_plan,
            reasoning_efforts=reasoning_efforts,
            reasoning=reasoning_plan.control == "reasoning_effort",
            can_stream=capabilities["streaming"], supports_schema=capabilities["json_schema"],
            vision=capabilities["vision"], supports_tools=capabilities["tools"],
            allows_system_prompt=capabilities["system_prompt"],
        )
        try:
            yield connected
        finally:
            connected._close()
