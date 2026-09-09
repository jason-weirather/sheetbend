from copy import deepcopy
import json
from pathlib import Path
import tomllib

from jsonschema import Draft202012Validator
import pytest

from sheetbend import Registry, __version__
from sheetbend.config import load_schema, select_config_path, validate_config
from sheetbend.errors import ConfigError, SelectionError


def test_packaged_schemas_are_valid():
    for name in ("config", "check"):
        Draft202012Validator.check_schema(load_schema(name))


def test_schema_defaults_and_ownership(config):
    original = deepcopy(config)
    registry = Registry.from_dict(config)
    assert config == original
    normalized = registry.to_dict()
    source = normalized["sources"]["local"]
    assert source["timeout_seconds"] == 30
    assert source["rate_limit"] == {}
    assert source["capabilities"]["json_schema"] is False
    assert source["capabilities"]["streaming"] is True
    config["sources"]["local"]["base_url"] = "https://different.example/v1"
    normalized["sources"].clear()
    assert registry.source().base_url == "http://127.0.0.1:8000/v1"
    assert registry.source() is registry.source("local")
    assert registry.source_names == ("local",)


def test_schema_is_independent_each_time():
    first = load_schema()
    first.clear()
    assert load_schema()["title"]
    with pytest.raises(ValueError):
        load_schema("../config")


def test_missing_implicit_default_is_empty_and_read_only(isolated_config):
    registry = Registry.from_file()
    assert registry.source_names == ()
    assert not registry.config_path.exists()
    assert list(isolated_config.iterdir()) == []
    with pytest.raises(SelectionError):
        registry.source()


def test_path_precedence(isolated_config, monkeypatch):
    monkeypatch.setenv("SHEETBEND_CONFIG", str(isolated_config / "env.toml"))
    assert select_config_path(isolated_config / "explicit.toml")[0].name == "explicit.toml"
    assert select_config_path()[0].name == "env.toml"
    with pytest.raises(ConfigError):
        Registry.from_file()
    monkeypatch.setenv("SHEETBEND_CONFIG", "")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_config / "xdg"))
    assert select_config_path()[0] == isolated_config / "xdg/sheetbend/config.toml"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-directory")
    assert select_config_path()[0] == isolated_config / ".config/sheetbend/config.toml"


def test_explicit_missing_or_invalid_file_does_not_fall_back(isolated_config):
    missing = isolated_config / "missing.toml"
    with pytest.raises(ConfigError):
        Registry.from_file(missing)
    invalid = isolated_config / "bad.toml"
    invalid.write_text("not valid [toml")
    with pytest.raises(ConfigError):
        Registry.from_file(invalid)
    with pytest.raises(ConfigError):
        Registry.from_file(isolated_config)


def test_broken_default_symlink_is_not_empty(isolated_config):
    path = isolated_config / ".config/sheetbend/config.toml"
    path.parent.mkdir(parents=True)
    path.symlink_to(isolated_config / "not-there")
    with pytest.raises(ConfigError):
        Registry.from_file()


def test_no_cwd_discovery(isolated_config, monkeypatch):
    monkeypatch.chdir(isolated_config)
    (isolated_config / "config.toml").write_text("bad = [")
    assert Registry.from_file().source_names == ()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_numbers_rejected(config, value):
    config["sources"]["local"]["timeout_seconds"] = value
    with pytest.raises(ConfigError):
        validate_config(config)


@pytest.mark.parametrize("updates", [
    {"unknown": True}, {"protocol": "other"}, {"auth": {"type": "none", "env": "KEY"}},
    {"auth": {"type": "bearer"}}, {"auth": {"type": "header", "env": "KEY"}},
    {"auth": {"type": "bearer", "env": "KEY", "api_key": "SECRET_VALUE"}},
    {"rate_limit": {"max_concurrent": 0}}, {"rate_limit": {"requests_per_minute": -1}},
    {"rate_limit": {"tokens_per_minute": 100}}, {"capabilities": {"vision": "yes"}},
    {"scope": "public"}, {"scope": "institutional"}, {"organization": "msk"},
    {"timeout_seconds": 0}, {"timeout_seconds": True}, {"default_model": "  "},
    {"base_url": "https://user:pass@example.org/v1"},
    {"base_url": "https://example.org/v1?api_key=SECRET_VALUE"},
    {"base_url": "https://example.org/v1#fragment"},
    {"base_url": "http://example.org/v1"}, {"base_url": "https://example.org:99999/v1"},
    {"base_url": "https://example.org:0/v1"}, {"base_url": "ftp://example.org/v1"},
    {"auth": {"type": "header", "env": "KEY", "header": "Host"}},
    {"auth": {"type": "header", "env": "KEY", "header": "Authorization"}},
])
def test_invalid_source(config, updates):
    config["sources"]["local"].update(updates)
    with pytest.raises(ConfigError) as caught:
        Registry.from_dict(config)
    assert "SECRET_VALUE" not in str(caught.value)


@pytest.mark.parametrize("url", ["http://localhost:8000/v1", "http://127.0.0.2/v1", "http://[::1]/v1"])
def test_loopback_http(config, url):
    config["sources"]["local"]["base_url"] = url
    assert Registry.from_dict(config).source().base_url == url


def test_explicit_insecure_http(config):
    config["sources"]["local"].update(
        base_url="http://internal.example/v1", allow_insecure_http=True
    )
    assert Registry.from_dict(config).source()


def test_no_default_inference_or_fallback(config):
    config.pop("default_source")
    registry = Registry.from_dict(config)
    with pytest.raises(SelectionError):
        registry.source()
    with pytest.raises(SelectionError):
        registry.source("missing")
    config["default_source"] = "missing"
    with pytest.raises(ConfigError):
        Registry.from_dict(config)


def test_scope_and_organization_filters(config):
    source = config["sources"]["local"]
    source.update(scope="institutional", organization="msk")
    registry = Registry.from_dict(config)
    assert registry.source(allowed_scopes=["institutional"], organization="msk")
    for scopes in (["local"], [], {"external"}):
        with pytest.raises(SelectionError):
            registry.source(allowed_scopes=scopes)
    with pytest.raises(SelectionError):
        registry.source(organization="another-institution")
    with pytest.raises(TypeError):
        registry.source(allowed_scopes="institutional")
    with pytest.raises(ValueError):
        registry.source(allowed_scopes=["typo"])


def test_example_validates():
    with Path("examples/config.toml").open("rb") as handle:
        config = tomllib.load(handle)
    assert Registry.from_dict(config).source_names == ("local", "msk", "external")


def test_toml_dates_are_not_silently_serialized(config):
    from datetime import date
    config["sources"]["local"]["description"] = date(2026, 9, 9)
    with pytest.raises(ConfigError):
        Registry.from_dict(config)


def test_version_has_single_authored_source():
    project = tomllib.loads(Path("pyproject.toml").read_text())
    assert __version__ == project["project"]["version"]
    assert json.loads(json.dumps(Registry.from_dict({"schema_version": 1, "sources": {}}).to_dict()))
