"""Unit tests for Sheetbend's diagnostic interpretation, not LLM integration."""

from contextlib import contextmanager
from types import SimpleNamespace

from jsonschema import Draft202012Validator
import pytest

from sheetbend import Registry
from sheetbend.config import load_schema
from sheetbend.errors import DependencyError


@pytest.mark.parametrize("test, text, expected", [
    ("text", "OK", True), ("text", "  ", False),
    ("schema", '{"ok":true}', True), ("schema", '{"ok":false}', False),
    ("schema", '{"ok":true,"extra":1}', False), ("schema", "not JSON", False),
    ("stream", "OK", True), ("stream", "", False),
])
def test_diagnostic_response_interpretation(config, monkeypatch, test, text, expected):
    source = Registry.from_dict(config).source()
    observed = []

    def prompt(value, **kwargs):
        observed.append((value, kwargs))
        return SimpleNamespace(text=lambda: text)

    @contextmanager
    def connect(model, *, probe_capability=None, probe=False, application=None, tool=None):
        assert probe is True
        assert application == "sheetbend"
        assert tool == f"check:{test}"
        assert model == "test-model"
        expected_capability = {"schema": "json_schema", "stream": "streaming"}.get(test)
        assert probe_capability == expected_capability
        yield SimpleNamespace(prompt=prompt)

    monkeypatch.setattr(source, "_connect", connect)
    result = source.check(test=test)
    assert result.ok is expected
    Draft202012Validator(load_schema("check")).validate(result.to_dict())
    assert observed[0][1]["stream"] is (test == "stream")
    if test == "schema":
        assert observed[0][1]["schema"]["required"] == ["ok"]
    assert source.to_dict()["models"]["test-model"]["capabilities"]["json_schema"] is False


def test_missing_integration_is_a_failed_probe(config, monkeypatch):
    source = Registry.from_dict(config).source()

    @contextmanager
    def unavailable(*args, **kwargs):
        raise DependencyError("Install the optional integration.")
        yield  # This is intentionally an unsuccessful context manager.

    monkeypatch.setattr(source, "_connect", unavailable)
    result = source.check(test="text")
    assert not result.ok
    assert "optional integration" in result.message
