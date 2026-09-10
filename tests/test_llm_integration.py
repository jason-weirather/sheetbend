"""Run with pip install -e '.[llm,dev]'. Uses actual LLM/SDK against loopback only."""

import json

import pytest

from sheetbend import Registry, Runtime
from sheetbend.errors import ConnectionClosedError

llm = pytest.importorskip("llm", reason="Optional LLM extra is not installed")


def source_at(config, endpoint):
    config["sources"]["local"]["base_url"] = endpoint["base_url"]
    return Registry.from_dict(config).source()


def test_real_llm_prompt_lazy_and_native_response(config, endpoint):
    source = source_at(config, endpoint)
    with source.connect() as model:
        assert isinstance(model, llm.KeyModel)
        response = model.prompt("Hello", stream=False)
        assert isinstance(response, llm.Response)
        assert endpoint["requests"] == []
        assert response.text() == "OK"
        assert response.usage().input == 3
    assert response.text() == "OK"
    assert len(endpoint["requests"]) == 1
    assert endpoint["requests"][0]["path"] == "/v1/chat/completions"
    assert not [r for r in Runtime().snapshot()["requests"] if r["state"] in ("running", "streaming")]


def test_real_llm_conversation(config, endpoint):
    source = source_at(config, endpoint)
    with source.connect() as model:
        conversation = model.conversation()
        assert conversation.prompt("First", stream=False).text() == "OK"
        assert conversation.prompt("Second", stream=False).text() == "OK"
    messages = endpoint["requests"][-1]["body"]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]


def test_schema_passes_through_and_is_locally_checked(config, endpoint):
    source = source_at(config, endpoint)
    assert source.to_dict()["models"]["test-model"]["capabilities"]["json_schema"] is False
    result = source.check(test="schema")
    assert result.ok, result.message
    wire = endpoint["requests"][-1]["body"]["response_format"]
    assert wire["type"] == "json_schema"
    assert wire["json_schema"]["schema"]["required"] == ["ok"]
    assert source.to_dict()["models"]["test-model"]["capabilities"]["json_schema"] is False
    endpoint["content"] = '{"ok":false}'
    assert not source.check(test="schema").ok
    endpoint["content"] = "not JSON"
    assert not source.check(test="schema").ok


def test_streaming_and_model_override(config, endpoint):
    source = source_at(config, endpoint)
    with source.connect(model="another-model") as model:
        assert "".join(model.prompt("Hello", stream=True)) == "OK"
    assert endpoint["requests"][-1]["body"]["model"] == "another-model"
    assert source.check(test="stream").ok


@pytest.mark.parametrize("auth, expected", [
    ({"type": "none"}, {}),
    ({"type": "bearer-placeholder"}, {"authorization": "Bearer sheetbend"}),
    ({"type": "bearer", "env": "TEST_KEY"}, {"authorization": "Bearer RIGHT_KEY"}),
    ({"type": "header", "env": "TEST_KEY", "header": "api-key"}, {"api-key": "RIGHT_KEY"}),
])
def test_actual_wire_auth_never_borrows_ambient_key(config, endpoint, monkeypatch, auth, expected):
    config["sources"]["local"]["auth"] = auth
    monkeypatch.setenv("TEST_KEY", "RIGHT_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "WRONG_KEY")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("OPENAI_ORG_ID", "WRONG_ORG")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "WRONG_PROJECT")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    source = source_at(config, endpoint)
    with source.connect() as model:
        assert model.prompt("Hello", stream=False).text() == "OK"
    headers = {k.lower(): v for k, v in endpoint["requests"][-1]["headers"].items()}
    for key, value in expected.items():
        assert headers[key] == value
    if "authorization" not in expected:
        assert "authorization" not in headers
    assert "WRONG" not in json.dumps(headers)


def test_lifetime_rejects_unconsumed_and_new_responses(config, endpoint):
    source = source_at(config, endpoint)
    with source.connect() as model:
        response = model.prompt("Deferred", stream=False)
    with pytest.raises(ConnectionClosedError):
        response.text()
    with pytest.raises(ConnectionClosedError):
        model.prompt("After close", stream=False).text()
    assert not endpoint["requests"]


def test_abandoned_stream_releases_concurrency_on_context_exit(config, endpoint):
    config["sources"]["local"]["rate_limit"] = {"max_concurrent": 1}
    source = source_at(config, endpoint)
    with source.connect() as model:
        response = model.prompt("Hello", stream=True)
        iterator = iter(response)
        assert next(iterator) == "OK"
        assert len([r for r in Runtime().snapshot()["requests"] if r["state"] in ("running", "streaming")]) == 1
    assert not [r for r in Runtime().snapshot()["requests"] if r["state"] in ("running", "streaming")]
    with source.connect() as model:
        assert model.prompt("Again", stream=False).text() == "OK"
    iterator.close()
    assert not [r for r in Runtime().snapshot()["requests"] if r["state"] in ("running", "streaming")]


def test_provider_failure_no_retries_and_redacted_diagnostics(config, endpoint):
    source = source_at(config, endpoint)
    endpoint["status"] = 429
    result = source.check(test="text")
    assert not result.ok
    assert "429" in result.message
    assert "DO_NOT_PRINT_PROVIDER_BODY" not in result.message
    assert len(endpoint["requests"]) == 1
    assert not [r for r in Runtime().snapshot()["requests"] if r["state"] in ("running", "streaming")]


def test_prompt_key_cannot_override_config(config, endpoint):
    source = source_at(config, endpoint)
    with source.connect() as model, pytest.raises(ValueError):
        model.prompt("Hello", key="UNEXPECTED_KEY", stream=False).text()
    assert not endpoint["requests"]


def test_two_connections_share_limiter(config, endpoint):
    source = source_at(config, endpoint)
    with source.connect() as first, source.connect() as second:
        assert first._admission is second._admission is source._admission


def test_ambient_custom_headers_rejected(config, endpoint, monkeypatch):
    from sheetbend.errors import CredentialError
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "X-Secret: UNRELATED_SECRET")
    source = source_at(config, endpoint)
    with pytest.raises(CredentialError), source.connect():
        pass
    assert not endpoint["requests"]


def test_real_tools_probe_has_no_callable_execution(config, endpoint):
    source = source_at(config, endpoint)
    result = source.check(test="tools")
    assert result.ok, result.message
    assert result.declared is False
    assert endpoint["requests"][-1]["body"]["tools"][0]["function"]["name"] == "sheetbend_probe"
    endpoint["tool_calls"] = False
    assert not source.check(test="tools").ok


def test_real_vision_probe_sends_only_synthetic_image(config, endpoint):
    source = source_at(config, endpoint)
    result = source.check(test="vision")
    assert result.ok, result.message
    parts = endpoint["requests"][-1]["body"]["messages"][-1]["content"]
    image = next(part for part in parts if part["type"] == "image_url")
    assert image["image_url"]["url"].startswith("data:image/png;base64,")
    endpoint["content"] = "red"
    assert not source.check(test="vision").ok


def test_real_telemetry_and_identity(config, endpoint):
    source = source_at(config, endpoint)
    with source.connect(application="downrange", tool="stain-qc") as model:
        response = model.prompt("Do not record this prompt", stream=False)
        assert Runtime().snapshot()["requests"] == []
        assert response.text() == "OK"
    record = Runtime().snapshot()["requests"][0]
    assert record["application"] == "downrange"
    assert record["tool"] == "stain-qc"
    assert (record["input_tokens"], record["output_tokens"]) == (3, 1)
    assert "Do not record" not in json.dumps(record)


def test_real_combined_report(config, endpoint):
    from jsonschema import Draft202012Validator
    from sheetbend.config import load_schema
    report = source_at(config, endpoint).check_all()
    assert report.ok, str(report)
    Draft202012Validator(load_schema("check-report")).validate(report.to_dict())
    assert len(endpoint["requests"]) == 6


@pytest.mark.parametrize("control, choice, expected", [
    ("reasoning_effort", "off", {"reasoning_effort": "none"}),
    ("reasoning_effort", "on", {"reasoning_effort": "medium"}),
    ("reasoning_effort", "low", {"reasoning_effort": "low"}),
    ("reasoning_effort", "provider", {}),
    ("chat_template_kwargs", "off", {"chat_template_kwargs": {"enable_thinking": False}}),
    ("chat_template_kwargs", "on", {"chat_template_kwargs": {"enable_thinking": True}}),
    ("fixed-off", "off", {}),
    ("fixed-on", "on", {}),
])
@pytest.mark.parametrize("stream", [False, True])
def test_real_reasoning_controls_on_wire(config, endpoint, control, choice, expected, stream):
    declaration = {"control": control}
    if control == "reasoning_effort":
        declaration["values"] = {"off": "none", "on": "medium", "low": "low"}
    config["sources"]["local"]["models"]["test-model"]["reasoning"] = declaration
    source = source_at(config, endpoint)
    with source.connect(reasoning=choice, application="test", tool="reasoning") as model:
        response = model.prompt("Do not rewrite this prompt.", stream=stream)
        assert response.text() == "OK"
    request = endpoint["requests"][-1]
    body = request["body"]
    actual = {key: body[key] for key in ("reasoning_effort", "chat_template_kwargs") if key in body}
    assert actual == expected
    assert "extra_body" not in body
    assert body["messages"] == [{"role": "user", "content": "Do not rewrite this prompt."}]
    assert "authorization" not in {key.lower() for key in request["headers"]}
    assert len(endpoint["requests"]) == 1


def test_real_configured_default_and_conversation(config, endpoint):
    config["sources"]["local"]["models"]["test-model"]["reasoning"] = {
        "control": "reasoning_effort", "default": "off", "values": {"off": "none", "on": "medium"},
    }
    source = source_at(config, endpoint)
    with source.connect() as model:
        conversation = model.conversation()
        assert conversation.prompt("First", stream=False).text() == "OK"
        assert conversation.prompt("Second", stream=False).text() == "OK"
    assert all(request["body"]["reasoning_effort"] == "none" for request in endpoint["requests"])
    with source.connect(reasoning="on") as model:
        assert model.prompt("Third", stream=False).text() == "OK"
    assert endpoint["requests"][-1]["body"]["reasoning_effort"] == "medium"


def test_real_bound_reasoning_rejects_native_override_before_request(config, endpoint):
    from sheetbend.errors import SelectionError
    config["sources"]["local"]["models"]["test-model"]["reasoning"] = {
        "control": "reasoning_effort", "values": {"off": "none", "on": "medium", "high": "high"},
    }
    source = source_at(config, endpoint)
    with source.connect(reasoning="off") as model:
        with pytest.raises(SelectionError, match="bound"):
            model.prompt("Hello", stream=False, options={"reasoning_effort": "high"}).text()
    assert endpoint["requests"] == []
    assert Runtime().snapshot()["requests"] == []


def test_real_native_effort_is_explicit_and_declared(config, endpoint):
    from sheetbend.errors import SelectionError
    config["sources"]["local"]["models"]["test-model"]["reasoning"] = {
        "control": "reasoning_effort", "values": {"off": "none", "on": "medium", "high": "high"},
    }
    source = source_at(config, endpoint)
    with source.connect(reasoning="provider") as model:
        assert model.prompt("Hello", stream=False, options={"reasoning_effort": "high"}).text() == "OK"
        with pytest.raises(SelectionError, match="not declared"):
            model.prompt("No low declaration", stream=False, options={"reasoning_effort": "low"}).text()
    assert len(endpoint["requests"]) == 1
    assert endpoint["requests"][0]["body"]["reasoning_effort"] == "high"


def test_real_check_all_uses_requested_reasoning(config, endpoint):
    config["sources"]["local"]["models"]["test-model"]["reasoning"] = {
        "control": "reasoning_effort", "values": {"off": "none", "on": "medium"},
    }
    report = source_at(config, endpoint).check_all(reasoning="off")
    assert report.ok, str(report)
    generations = [r for r in endpoint["requests"] if r["body"] is not None]
    assert len(generations) == 5
    assert all(r["body"]["reasoning_effort"] == "none" for r in generations)
