"""Small synthetic diagnostics, kept separate from ordinary inference."""

import json
import time
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from .check_result import CheckResult
from .errors import ProbeError, SheetbendError
from .source import Source

_TESTS = {"models", "text", "schema", "stream"}
_PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean", "enum": [True]}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _probe_generation(source: Source, *, test: str, model: str) -> None:
    capability = {"schema": "json_schema", "stream": "streaming"}.get(test)
    with source._connect(model, probe_capability=capability) as connected:
        options: dict[str, Any] = {"stream": test == "stream"}
        if test == "schema":
            options["schema"] = _PROBE_SCHEMA
            prompt = 'Return a JSON object whose only field is "ok" with the value true.'
        else:
            prompt = "This is a connectivity test. Reply with the word OK."
        try:
            response = connected.prompt(prompt, **options)
            text = response.text()
        except Exception as exc:
            # Diagnostics are explicitly failure-collecting operations. Do not
            # include provider bodies, exception reprs, prompts, or credentials.
            code = getattr(exc, "status_code", None)
            if isinstance(code, int):
                message = f"Provider returned HTTP {code}."
            else:
                message = f"Generation failed ({type(exc).__name__}); response details withheld."
            raise ProbeError(message) from exc
    if not text.strip():
        raise ProbeError("The generation returned no text.")
    if test == "schema":
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise ProbeError("The schema probe did not return JSON.") from exc
        if not Draft202012Validator(_PROBE_SCHEMA).is_valid(value):
            raise ProbeError("The returned JSON does not satisfy the probe schema.")


def check_source(source: Source, *, test: str, model: str) -> CheckResult:
    if test not in _TESTS:
        raise ValueError(f"test must be one of {sorted(_TESTS)}.")
    started = time.monotonic()
    model_ids: tuple[str, ...] = ()
    try:
        if test == "models":
            model_ids = tuple(source.list_models())
            message = f"Listed {len(model_ids)} model(s); inference was not tested."
        else:
            _probe_generation(source, test=test, model=model)
            message = {
                "text": "Received nonempty text from a synthetic prompt.",
                "schema": "Received JSON matching the probe schema; not proof of general schema enforcement.",
                "stream": "Consumed a streaming response with nonempty text.",
            }[test]
        ok = True
    except httpx.HTTPStatusError as exc:
        ok, message = False, f"Provider returned HTTP {exc.response.status_code}."
    except httpx.RequestError as exc:
        ok, message = False, f"Transport failed ({type(exc).__name__}); details withheld."
    except SheetbendError as exc:
        ok, message = False, str(exc)
    return CheckResult(
        source=source.name,
        model=model,
        test=test,
        ok=ok,
        elapsed_seconds=time.monotonic() - started,
        message=message,
        model_ids=model_ids,
    )
