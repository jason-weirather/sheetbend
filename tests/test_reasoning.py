"""Declared reasoning semantics, offline selection, and CLI propagation."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json

from click.testing import CliRunner
from jsonschema import Draft202012Validator
import pytest

from sheetbend import Registry
from sheetbend.check_result import CheckResult
from sheetbend.cli import main
from sheetbend.config import load_schema
from sheetbend.errors import ConfigError, SelectionError
from sheetbend.reasoning import reasoning_choices


def _source(config, declaration):
    config["sources"]["local"]["models"]["test-model"]["reasoning"] = declaration
    return Registry.from_dict(config).source()


def test_existing_configuration_keeps_provider_behavior(config):
    original = deepcopy(config)
    source = Registry.from_dict(config).source()
    plan = source.resolve_reasoning()
    assert plan.requested is None
    assert plan.selected == "provider"
    assert plan.origin == "model-default"
    assert plan.control == "unknown"
    assert plan.parameter is plan.value is None
    assert config == original
    assert source._admission._runtime is None
    Draft202012Validator(load_schema("reasoning-plan")).validate(plan.to_dict())


@pytest.mark.parametrize("control, choice, parameter, value", [
    ("unknown", "provider", None, None),
    ("fixed-off", "off", None, None),
    ("fixed-off", "provider", None, None),
    ("fixed-on", "on", None, None),
    ("fixed-on", "provider", None, None),
    ("chat_template_kwargs", "provider", None, None),
    ("chat_template_kwargs", "off", "chat_template_kwargs.enable_thinking", False),
    ("chat_template_kwargs", "on", "chat_template_kwargs.enable_thinking", True),
])
def test_known_fixed_and_boolean_controls(config, control, choice, parameter, value):
    plan = _source(config, {"control": control}).resolve_reasoning(choice)
    assert plan.selected == plan.requested == choice
    assert plan.origin == "caller"
    assert plan.parameter == parameter
    assert plan.value == value
    assert type(plan.value) is type(value)
    Draft202012Validator(load_schema("reasoning-plan")).validate(plan.to_dict())


@pytest.mark.parametrize("choice, expected", [
    ("off", "none"), ("on", "medium"), ("low", "low"),
    ("medium", "medium"), ("high", "high"), ("provider", None),
])
def test_effort_mapping_is_explicit(config, choice, expected):
    source = _source(config, {
        "control": "reasoning_effort",
        "values": {"off": "none", "on": "medium", "low": "low", "medium": "medium", "high": "high"},
    })
    plan = source.resolve_reasoning(choice)
    assert plan.value == expected
    assert plan.parameter == (None if choice == "provider" else "reasoning_effort")
    Draft202012Validator(load_schema("reasoning-plan")).validate(plan.to_dict())


@pytest.mark.parametrize("control, choice", [
    ("unknown", "off"), ("unknown", "on"), ("unknown", "low"),
    ("fixed-on", "off"), ("fixed-on", "low"), ("fixed-off", "on"),
    ("fixed-off", "low"), ("chat_template_kwargs", "low"),
    ("chat_template_kwargs", "medium"), ("chat_template_kwargs", "high"),
])
def test_unsupported_is_not_silently_downgraded(config, control, choice):
    config["sources"]["local"]["auth"] = {"type": "bearer", "env": "ABSENT_KEY"}
    source = _source(config, {"control": control})
    with pytest.raises(SelectionError, match="no fallback"), source.connect(reasoning=choice):
        pass
    assert source._admission._runtime is None


def test_off_is_not_minimum_available_reasoning(config):
    source = _source(config, {"control": "reasoning_effort", "values": {"on": "low", "low": "low"}})
    with pytest.raises(SelectionError):
        source.resolve_reasoning("off")
    assert source.resolve_reasoning("on").value == "low"
    with pytest.raises(SelectionError):
        source.resolve_reasoning("high")


def test_default_caller_override_and_provider_escape(config):
    source = _source(config, {
        "control": "reasoning_effort", "default": "off", "values": {"off": "none", "on": "medium"},
    })
    assert source.resolve_reasoning().selected == "off"
    assert source.resolve_reasoning().origin == "model-default"
    assert source.resolve_reasoning("on").value == "medium"
    plan = source.resolve_reasoning("provider")
    assert plan.parameter is None
    assert plan.origin == "caller"
    assert source.to_dict()["models"]["test-model"]["reasoning"]["default"] == "off"


def test_second_model_does_not_borrow_reasoning_declaration(config):
    source = _source(config, {"control": "reasoning_effort", "values": {"off": "none"}})
    assert source.resolve_reasoning("off").value == "none"
    with pytest.raises(SelectionError):
        source.resolve_reasoning("off", model="another-model")
    definition = source._model_definition("unlisted", probe=True)
    assert definition["reasoning"] == {"control": "unknown", "default": "provider"}
    result = source.check(test="text", model="unlisted", reasoning="off")
    assert not result.ok
    assert "unknown" in result.message
    assert source._admission._runtime is None


def test_plan_is_owned_and_printable(config):
    source = _source(config, {"control": "chat_template_kwargs", "default": "off"})
    plan = source.resolve_reasoning()
    data = plan.to_dict()
    data["value"] = True
    assert plan.value is False
    with pytest.raises(FrozenInstanceError):
        plan.selected = "on"
    assert "reasoning=off" in str(plan)
    assert "enable_thinking=False" in str(plan)
    assert "Source" not in str(plan)


@pytest.mark.parametrize("declaration", [
    True, {"control": "typo"}, {"default": "off"},
    {"control": "fixed-on", "default": "off"},
    {"control": "fixed-off", "default": "on"},
    {"control": "chat_template_kwargs", "default": "low"},
    {"control": "unknown", "values": {"off": "none"}},
    {"control": "fixed-off", "values": {"off": "none"}},
    {"control": "chat_template_kwargs", "values": {"off": False}},
    {"control": "reasoning_effort"},
    {"control": "reasoning_effort", "values": {}},
    {"control": "reasoning_effort", "values": {"off": "low"}},
    {"control": "reasoning_effort", "values": {"on": "none"}},
    {"control": "reasoning_effort", "values": {"on": True}},
    {"control": "reasoning_effort", "values": {"low": "minimal"}},
    {"control": "reasoning_effort", "values": {"provider": "low"}},
    {"control": "reasoning_effort", "default": "high", "values": {"on": "medium"}},
    {"control": "reasoning_effort", "values": {"off": "none"}, "parameter": "anything"},
])
def test_invalid_declarations_fail_at_config_barrier(config, declaration):
    with pytest.raises(ConfigError):
        _source(config, declaration)


@pytest.mark.parametrize("choice", [True, False, 0, "none", "auto", "OFF", {}, ["off"]])
def test_invalid_caller_choice_is_not_coerced(config, choice):
    source = Registry.from_dict(config).source()
    with pytest.raises(ValueError):
        source.resolve_reasoning(choice)
    with pytest.raises(ValueError):
        source.check_all(reasoning=choice)
    assert source._admission._runtime is None


def test_catalog_has_no_reasoning_setting(config):
    source = Registry.from_dict(config).source()
    with pytest.raises(ValueError, match="does not generate"):
        source.check(reasoning="off")
    result = CliRunner().invoke(main, ["check", "local", "--reasoning", "off"])
    assert result.exit_code == 2
    assert "catalog does not generate" in result.output


def test_connection_passes_reasoning_choice(config, monkeypatch):
    from contextlib import contextmanager
    source = _source(config, {"control": "chat_template_kwargs"})
    seen = []

    @contextmanager
    def connect(model, **kwargs):
        seen.append((model, kwargs))
        yield "connected"

    monkeypatch.setattr(source, "_connect", connect)
    with source.connect(reasoning="off", application="jupyter", tool="bench") as model:
        assert model == "connected"
    assert seen[0][1] == {"reasoning": "off", "application": "jupyter", "tool": "bench"}


def test_check_all_propagates_to_generations_only(config, monkeypatch):
    source = _source(config, {"control": "chat_template_kwargs"})
    seen = []

    def check(*, test, model, reasoning):
        seen.append((test, reasoning))
        return CheckResult("local", model, test, True, 0, "synthetic", 1, None)

    monkeypatch.setattr(source, "check", check)
    report = source.check_all(reasoning="off")
    assert report.ok
    assert seen == [("models", None), ("text", "off"), ("schema", "off"),
                    ("stream", "off"), ("tools", "off"), ("vision", "off")]


def test_cli_forwards_reasoning_to_library(tmp_path, monkeypatch):
    from sheetbend.source import Source
    path = tmp_path / "config.toml"
    path.write_text('''schema_version = 2
[sources.test]
protocol = "openai-compatible"
base_url = "http://127.0.0.1:8000/v1"
default_model = "test"
scope = "local"
auth = {type = "none"}
[sources.test.models.test.reasoning]
control = "fixed-off"
''')
    seen = []

    def check(self, *, test, model, reasoning):
        seen.append((test, model, reasoning))
        return CheckResult(self.name, "test", test, True, 0, "synthetic", 1, None)

    monkeypatch.setattr(Source, "check", check)
    result = CliRunner().invoke(main, ["--config-path", str(path), "check", "test",
                                      "--test", "text", "--reasoning", "off", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"]
    assert seen == [("text", None, "off")]


def test_schema_vocabulary_matches_public_plan_and_cli():
    config = load_schema()
    plan = load_schema("reasoning-plan")
    assert config["$defs"]["reasoning_choice"] == plan["$defs"]["choice"]
    assert config["$defs"]["reasoning"]["properties"]["control"]["enum"] == plan["properties"]["control"]["enum"]
    result = CliRunner().invoke(main, ["check", "--help"])
    assert result.exit_code == 0
    assert "--reasoning" in result.output
    assert "configured default" in result.output
    for choice in reasoning_choices():
        assert choice in result.output
    result = CliRunner().invoke(main, ["schema", "--name", "reasoning-plan"])
    assert result.exit_code == 0
    assert json.loads(result.output) == plan


def test_reasoning_changes_do_not_create_another_resource_budget(config, tmp_path):
    path = tmp_path / "config.toml"
    first = Registry(config, config_path=path).source()
    changed = deepcopy(config)
    changed["sources"]["local"]["models"]["test-model"]["reasoning"] = {
        "control": "chat_template_kwargs", "default": "off",
    }
    second = Registry(changed, config_path=path).source()
    assert first._admission.bucket == second._admission.bucket
    assert first._admission.fingerprint == second._admission.fingerprint


def test_unavailable_reasoning_diagnostic_stays_structured(config):
    result = Registry.from_dict(config).source().check(test="text", reasoning="off")
    assert not result.ok
    assert "reasoning='off'" in result.message
    assert "unknown" in result.message
    Draft202012Validator(load_schema("check")).validate(result.to_dict())
