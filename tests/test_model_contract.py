from contextlib import contextmanager
from copy import deepcopy
import json

from click.testing import CliRunner
from jsonschema import Draft202012Validator
import pytest

from sheetbend import Registry
from sheetbend.cli import main
from sheetbend.config import load_schema
from sheetbend.errors import ConfigError, SelectionError


def test_capabilities_are_per_model_and_owned(config):
    config["sources"]["local"]["models"] = {
        "test-model": {"capabilities": {"json_schema": True}},
        "other/model:1.0": {"capabilities": {"vision": True}},
    }
    original = deepcopy(config)
    source = Registry.from_dict(config).source()
    assert config == original
    assert source.model_names == ("test-model", "other/model:1.0")
    models = source.to_dict()["models"]
    assert models["test-model"]["capabilities"]["json_schema"]
    assert not models["other/model:1.0"]["capabilities"]["json_schema"]
    assert models["other/model:1.0"]["capabilities"]["vision"]
    models.clear()
    assert source.model_names


def test_schema_one_and_source_capabilities_are_removed(config):
    config["schema_version"] = 1
    with pytest.raises(ConfigError):
        Registry.from_dict(config)
    config["schema_version"] = 2
    config["sources"]["local"]["capabilities"] = {"json_schema": True}
    with pytest.raises(ConfigError):
        Registry.from_dict(config)


def test_unconfigured_default_model_is_invalid(config):
    config["sources"]["local"]["default_model"] = "not-declared"
    with pytest.raises(ConfigError, match="default_model"):
        Registry.from_dict(config)


def test_requirements_fail_before_import_creds_or_runtime(config):
    config["sources"]["local"]["auth"] = {"type": "bearer", "env": "ABSENT_ENV_VAR"}
    source = Registry.from_dict(config).source()
    with pytest.raises(SelectionError, match="json_schema"), source.connect(requires={"json_schema"}):
        pass
    with pytest.raises(SelectionError, match="not configured"), source.connect(model="not-declared"):
        pass
    with pytest.raises(ValueError, match="Unknown"), source.connect(requires={"typo"}):
        pass
    with pytest.raises(TypeError), source.connect(requires="vision"):
        pass
    assert source._admission._runtime is None


def test_valid_requirements_and_identity_forwarded(config, monkeypatch):
    config["sources"]["local"]["models"]["test-model"] = {"capabilities": {"json_schema": True}}
    source = Registry.from_dict(config).source()
    seen = []
    @contextmanager
    def connect(model, **kwargs):
        seen.append((model, kwargs))
        yield "sentinel"
    monkeypatch.setattr(source, "_connect", connect)
    with source.connect(requires=["json_schema"], application="downrange", tool="stain-qc") as model:
        assert model == "sentinel"
    assert seen == [("test-model", {"application": "downrange", "tool": "stain-qc"})]


def test_probe_of_unknown_model_does_not_inherit_default_model(config):
    config["sources"]["local"]["models"]["test-model"] = {"capabilities": {"vision": True}}
    source = Registry.from_dict(config).source()
    assert source._model_definition("unlisted", probe=True)["capabilities"]["vision"] is False
    assert "unlisted" not in source.model_names


def test_external_requires_explicit_permission(config):
    config["sources"]["local"]["scope"] = "external"
    registry = Registry.from_dict(config)
    with pytest.raises(SelectionError):
        registry.source()
    assert registry.source(allowed_scopes={"external"})
    with pytest.raises(TypeError):
        registry.source(allowed_scopes=None)


def test_all_report_collects_failures_with_no_schema_changes(config, monkeypatch):
    source = Registry.from_dict(config).source()
    original = source.to_dict()
    @contextmanager
    def missing(*args, **kwargs):
        from sheetbend.errors import DependencyError
        raise DependencyError("Deliberate diagnostic failure")
        yield
    monkeypatch.setattr(source, "_connect", missing)
    monkeypatch.setattr(source, "list_models", lambda **kwargs: ["test-model"])
    report = source.check_all()
    assert not report.ok
    assert len(report.results) == 6
    assert report.results[0].ok
    assert [r.test for r in report.results] == ["models", "text", "schema", "stream", "tools", "vision"]
    Draft202012Validator(load_schema("check-report")).validate(report.to_dict())
    assert report.results[2].declared is False
    assert source.to_dict() == original


def test_top_json_ignores_invalid_configuration(tmp_path, monkeypatch):
    bad = tmp_path / "invalid.toml"
    bad.write_text("this = [")
    monkeypatch.setenv("SHEETBEND_CONFIG", str(bad))
    result = CliRunner().invoke(main, ["top", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    Draft202012Validator(load_schema("activity")).validate(data)
    assert data["requests"] == []
    assert CliRunner().invoke(main, ["top", "--once"]).exit_code == 0
    result = CliRunner().invoke(main, ["top"])
    assert result.exit_code != 0
    assert "--once" in result.output


def test_all_and_test_are_mutually_exclusive():
    result = CliRunner().invoke(main, ["check", "local", "--all", "--test", "text"])
    assert result.exit_code == 2
    assert "not both" in result.output


@pytest.mark.parametrize("application,tool", [("", None), ("\n", None), ("x"*81, None), ("valid", "\x1b[31m")])
def test_invalid_caller_labels_rejected_before_integration(config, application, tool):
    with pytest.raises(ValueError), Registry.from_dict(config).source().connect(application=application, tool=tool):
        pass


@pytest.mark.parametrize("value", ["nan", "inf"])
def test_top_interval_must_be_finite(value):
    result = CliRunner().invoke(main, ["top", "--once", "--refresh-seconds", value])
    assert result.exit_code == 2
    assert "finite" in result.output


def test_mac_example_validates():
    from pathlib import Path
    import tomllib
    data = tomllib.loads(Path("examples/mac-local-endpoint.toml").read_text())
    source = Registry.from_dict(data).source()
    assert source.name == "laptop"
    assert source.default_model == "qwen3.5:4b"
    assert source.scope == "local"
