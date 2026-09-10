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

    class Chat:
        def __init__(self, **kwargs):
            self.model_id = kwargs["model_id"]
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
