"""Fixed synthetic diagnostics. No files, arbitrary tool execution, or config edits."""

import json
import struct
import time
from typing import Any
import zlib

import httpx
from jsonschema import Draft202012Validator

from .check_result import CheckResult
from .errors import ProbeError, SheetbendError
from .source import Source

TESTS = ("models", "text", "schema", "stream", "tools", "vision")
_CAPABILITY = {"schema": "json_schema", "stream": "streaming", "tools": "tools", "vision": "vision"}
_PROBE_SCHEMA = {
    "type": "object", "properties": {"ok": {"type": "boolean", "enum": [True]}},
    "required": ["ok"], "additionalProperties": False,
}


def _probe_image() -> bytes:
    """A 64x64 green RGB PNG generated in memory using only the standard library."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    header = struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0)
    rows = (b"\x00" + b"\x00\xff\x00" * 64) * 64
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _probe_generation(
    source: Source, *, test: str, model: str, reasoning: str | None = None,
) -> None:
    capability = _CAPABILITY.get(test)
    with source._connect(
        model, probe_capability=capability, probe=True, reasoning=reasoning,
        application="sheetbend", tool=f"check:{test}",
    ) as connected:
        options: dict[str, Any] = {"stream": test == "stream"}
        prompt = "This is a connectivity test. Reply with the word OK."
        if test == "schema":
            options["schema"] = _PROBE_SCHEMA
            prompt = 'Return a JSON object whose only field is "ok" with the value true.'
        elif test == "tools":
            import llm
            options["tools"] = [llm.Tool(
                name="sheetbend_probe", description="Report a successful synthetic connectivity test.",
                input_schema=_PROBE_SCHEMA,
            )]
            prompt = "Call sheetbend_probe once with ok set to true. Do not answer in prose."
        elif test == "vision":
            import llm
            options["attachments"] = [llm.Attachment(type="image/png", content=_probe_image())]
            prompt = "What is the dominant color of this image? Reply with one English color word only."
        try:
            response = connected.prompt(prompt, **options)
            text = response.text()
            if test == "tools":
                calls = response.tool_calls()
                if (len(calls) != 1 or calls[0].name != "sheetbend_probe"
                        or not Draft202012Validator(_PROBE_SCHEMA).is_valid(calls[0].arguments)):
                    raise ProbeError("Expected one sheetbend_probe call with arguments matching the probe schema.")
                return  # The proposed tool is never executed.
        except ProbeError:
            raise
        except Exception as exc:
            code = getattr(exc, "status_code", None)
            message = (f"Provider returned HTTP {code}." if isinstance(code, int)
                       else f"Generation failed ({type(exc).__name__}); response details withheld.")
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
    elif test == "vision" and text.strip().strip(".! \n").lower() != "green":
        raise ProbeError("The model did not identify the synthetic image's dominant color.")


def check_source(
    source: Source, *, test: str, model: str, reasoning: str | None = None,
) -> CheckResult:
    if test not in TESTS:
        raise ValueError(f"test must be one of {list(TESTS)}.")
    started = time.monotonic()
    observed_at = time.time()
    capability = _CAPABILITY.get(test)
    declared = (source.to_dict()["models"].get(model, {}).get("capabilities", {}).get(capability)
                if capability else None)
    model_ids: tuple[str, ...] = ()
    try:
        if test == "models":
            model_ids = tuple(source.list_models(application="sheetbend", tool="check:models"))
            message = f"Listed {len(model_ids)} model(s); inference was not tested."
        else:
            _probe_generation(source, test=test, model=model, reasoning=reasoning)
            message = {
                "text": "Received nonempty text from a synthetic prompt.",
                "schema": "Received JSON matching the probe schema; not proof of general schema enforcement.",
                "stream": "Consumed a streaming response with nonempty text.",
                "tools": "Received one valid synthetic tool call; no tool was executed.",
                "vision": "Identified a synthetic image's dominant color; not a vision benchmark.",
            }[test]
        ok = True
    except httpx.HTTPStatusError as exc:
        ok, message = False, f"Provider returned HTTP {exc.response.status_code}."
    except httpx.RequestError as exc:
        ok, message = False, f"Transport failed ({type(exc).__name__}); details withheld."
    except SheetbendError as exc:
        ok, message = False, str(exc)
    except Exception as exc:
        # Multi-probe diagnostics explicitly collect failures, unlike inference.
        ok, message = False, f"Probe failed ({type(exc).__name__}); details withheld."
    return CheckResult(
        source=source.name, model=model, test=test, ok=ok,
        elapsed_seconds=time.monotonic() - started, message=message,
        observed_at=observed_at, declared=declared, model_ids=model_ids,
    )
