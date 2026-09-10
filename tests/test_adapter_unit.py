"""Test Sheetbend's execution hooks with a small test double, NOT real LLM integration."""

from concurrent.futures import ThreadPoolExecutor
from importlib import util
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from sheetbend import Registry, Runtime
from sheetbend.errors import ConnectionClosedError


@pytest.fixture
def adapter(monkeypatch):
    llm = ModuleType("llm")
    llm.Prompt = llm.Response = llm.Conversation = llm.KeyModel = object
    plugins = ModuleType("llm.default_plugins")
    models = ModuleType("llm.default_plugins.openai_models")
    sdk = ModuleType("openai")
    sdk.OpenAI = object

    class Omit:
        pass

    sdk.Omit = Omit

    class Chat:
        def __init__(self, **kwargs):
            self.model_id = kwargs["model_id"]
        def build_kwargs(self, prompt, stream):
            return {"stream_options": {"include_usage": True}} if stream else {}
        def execute(self, prompt, stream, response, conversation=None, key=None):
            response.executed += 1
            if prompt == "error":
                raise RuntimeError("PRIVATE PROVIDER ERROR")
            yield "hello"
            yield " world"
            response.input_tokens = 3
            response.output_tokens = 2
    models.Chat = Chat
    for name, module in (("llm", llm), ("llm.default_plugins", plugins),
                         ("llm.default_plugins.openai_models", models), ("openai", sdk)):
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).parents[1] / "src/sheetbend/llm_adapter.py"
    spec = util.spec_from_file_location("sheetbend._adapter_under_test", path)
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _connected(adapter, config, tmp_path, **labels):
    config["sources"]["local"]["rate_limit"] = {"max_concurrent": 1}
    source = Registry(config, config_path=tmp_path / "config.toml").source()
    return adapter._ConnectedChat(
        client=object(), admission=source._admission, model_id="test-model",
        application=labels.get("application", "notebook"), tool=labels.get("tool"),
        omit_authorization=labels.get("omit_authorization", True),
        reasoning_plan=source.resolve_reasoning(labels.get("reasoning")),
        reasoning_efforts=source.to_dict()["models"][source.default_model]["reasoning"].get("values", {}).values(),
    )


def _response():
    return SimpleNamespace(input_tokens=None, output_tokens=None, executed=0)


def test_lazy_execution_and_completed_usage(adapter, config, tmp_path):
    model = _connected(adapter, config, tmp_path, application="downrange", tool="stain-qc")
    response = _response()
    execution = model.execute("hello", False, response)
    assert response.executed == 0
    assert Runtime().snapshot()["requests"] == []
    assert "".join(execution) == "hello world"
    row = Runtime().snapshot()["requests"][0]
    assert row["state"] == "done"
    assert (row["input_tokens"], row["output_tokens"]) == (3, 2)
    assert row["application"] == "downrange"
    assert row["tool"] == "stain-qc"
    model._close()


def test_paused_stream_stays_active_then_context_cancels(adapter, config, tmp_path):
    model = _connected(adapter, config, tmp_path)
    execution = model.execute("hello", True, _response())
    assert next(execution) == "hello"
    row = Runtime().snapshot()["requests"][0]
    assert row["state"] == "streaming"
    assert row["input_tokens"] is None
    model._close()
    assert Runtime().snapshot()["requests"][0]["state"] == "cancelled"
    with pytest.raises(ConnectionClosedError):
        next(execution)
    assert Runtime().snapshot()["requests"][0]["state"] == "cancelled"


def test_generator_close_releases_slot_without_closing_connection(adapter, config, tmp_path):
    model = _connected(adapter, config, tmp_path)
    execution = model.execute("hello", True, _response())
    next(execution)
    execution.close()
    assert Runtime().snapshot()["requests"][0]["state"] == "cancelled"
    assert "".join(model.execute("another", False, _response())) == "hello world"
    model._close()


def test_error_releases_slot_and_does_not_record_exception(adapter, config, tmp_path):
    model = _connected(adapter, config, tmp_path)
    with pytest.raises(RuntimeError):
        list(model.execute("error", False, _response()))
    data = Runtime().snapshot()
    assert data["requests"][0]["state"] == "error"
    assert "PRIVATE" not in str(data)
    assert list(model.execute("another", False, _response()))
    model._close()


def test_close_wakes_pending_admission_without_late_execution(adapter, config, tmp_path):
    first = _connected(adapter, config, tmp_path)
    second = _connected(adapter, config, tmp_path)
    streaming = first.execute("hello", True, _response())
    next(streaming)
    response = _response()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(list, second.execute("never sent", False, response))
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if any(r["state"] == "waiting" for r in Runtime().snapshot()["requests"]):
                break
            time.sleep(0.01)
        second._close()
        with pytest.raises(ConnectionClosedError):
            future.result(timeout=3)
    assert response.executed == 0
    first._close()
    streaming.close()


def test_connection_after_fork_is_rejected(adapter, config, tmp_path):
    model = _connected(adapter, config, tmp_path)
    model._pid -= 1
    with pytest.raises(ConnectionClosedError):
        list(model.execute("hello", False, _response()))
    assert not Runtime().snapshot()["requests"]
    model._close()


def test_independent_connection_labels(adapter, config, tmp_path):
    first = _connected(adapter, config, tmp_path, application="downrange", tool="qc")
    second = _connected(adapter, config, tmp_path, application="notebook", tool="exploration")
    list(first.execute("hi", False, _response()))
    list(second.execute("hi", False, _response()))
    rows = Runtime().snapshot()["requests"]
    assert {(r["application"], r["tool"]) for r in rows} == {("downrange", "qc"), ("notebook", "exploration")}
    first._close()
    second._close()


def test_authorization_omission_is_a_per_request_override(adapter, config, tmp_path):
    model = _connected(adapter, config, tmp_path, omit_authorization=True)
    kwargs = model.build_kwargs("hello", False)
    assert isinstance(kwargs["extra_headers"]["Authorization"], adapter.openai.Omit)
    model._close()


def test_bearer_authorization_does_not_add_request_omission(adapter, config, tmp_path):
    model = _connected(adapter, config, tmp_path, omit_authorization=False)
    assert "extra_headers" not in model.build_kwargs("hello", False)
    model._close()


@pytest.mark.parametrize("control, choice, expected", [
    ("reasoning_effort", "off", {"reasoning_effort": "none"}),
    ("reasoning_effort", "on", {"reasoning_effort": "medium"}),
    ("reasoning_effort", "provider", {}),
    ("chat_template_kwargs", "off", {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}),
    ("chat_template_kwargs", "on", {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}}),
    ("fixed-off", "off", {}),
    ("fixed-on", "on", {}),
])
@pytest.mark.parametrize("stream", [False, True])
def test_reasoning_payload_preserves_auth_and_streaming(adapter, config, tmp_path, control, choice, expected, stream):
    declaration = {"control": control}
    if control == "reasoning_effort":
        declaration["values"] = {"off": "none", "on": "medium"}
    config["sources"]["local"]["models"]["test-model"]["reasoning"] = declaration
    model = _connected(adapter, config, tmp_path, reasoning=choice)
    kwargs = model.build_kwargs("hello", stream)
    assert isinstance(kwargs.pop("extra_headers")["Authorization"], adapter.openai.Omit)
    if stream:
        assert kwargs.pop("stream_options") == {"include_usage": True}
    assert kwargs == expected
    model._close()


def test_native_effort_cannot_override_bound_reasoning(adapter, config, tmp_path):
    from sheetbend.errors import SelectionError
    config["sources"]["local"]["models"]["test-model"]["reasoning"] = {
        "control": "reasoning_effort", "default": "off", "values": {"off": "none", "on": "high"},
    }
    model = _connected(adapter, config, tmp_path)
    prompt = SimpleNamespace(options=SimpleNamespace(reasoning_effort="high"))
    with pytest.raises(SelectionError, match="bound"):
        list(model.execute(prompt, False, _response()))
    assert not Runtime().snapshot()["requests"]
    model._close()


def test_native_provider_effort_requires_declared_value(adapter, config, tmp_path):
    from sheetbend.errors import SelectionError
    config["sources"]["local"]["models"]["test-model"]["reasoning"] = {
        "control": "reasoning_effort", "values": {"off": "none", "on": "medium", "high": "high"},
    }
    model = _connected(adapter, config, tmp_path, reasoning="provider")
    accepted = SimpleNamespace(options=SimpleNamespace(reasoning_effort="high"))
    model._validate_native_reasoning(accepted)
    rejected = SimpleNamespace(options=SimpleNamespace(reasoning_effort="low"))
    with pytest.raises(SelectionError, match="not declared"):
        list(model.execute(rejected, False, _response()))
    assert not Runtime().snapshot()["requests"]
    model._close()


@pytest.mark.parametrize("control, expected_flag", [
    ("unknown", False), ("fixed-off", False), ("fixed-on", False),
    ("reasoning_effort", True), ("chat_template_kwargs", False),
])
def test_connection_constructor_wires_declared_reasoning(adapter, config, monkeypatch, control, expected_flag):
    """Constructor wiring with a fake SDK; actual socket assertions live in integration tests."""
    from contextlib import contextmanager
    declaration = {"control": control}
    if control == "reasoning_effort":
        declaration["values"] = {"off": "none", "on": "medium"}
    config["sources"]["local"]["models"]["test-model"]["reasoning"] = declaration
    source = Registry.from_dict(config).source()
    seen = {}

    @contextmanager
    def client(**kwargs):
        yield SimpleNamespace()

    class Connected:
        def __init__(self, **kwargs):
            seen.update(kwargs)
        def _close(self):
            seen["closed"] = True

    monkeypatch.setattr(adapter.openai, "DefaultHttpxClient", client, raising=False)
    monkeypatch.setattr(adapter.openai, "OpenAI", client)
    monkeypatch.setattr(adapter, "_ConnectedChat", Connected)
    with adapter.connected_model(
        source, model=source.default_model,
        capabilities=source.to_dict()["models"][source.default_model]["capabilities"],
        reasoning_plan=source.resolve_reasoning(),
        reasoning_efforts=declaration.get("values", {}).values(),
    ):
        assert seen["reasoning"] is expected_flag
        assert seen["reasoning_plan"].control == control
        assert seen["omit_authorization"] is True
    assert seen["closed"]
