import json
import ssl

from jsonschema import Draft202012Validator
import pytest

from sheetbend import Registry
from sheetbend.config import load_schema
from sheetbend.errors import CredentialError


def test_auth_is_lazy_and_not_in_inspection(config, monkeypatch):
    config["sources"]["local"]["auth"] = {"type": "bearer", "env": "TEST_KEY"}
    monkeypatch.delenv("TEST_KEY", raising=False)
    registry = Registry.from_dict(config)
    source = registry.source()
    assert "TEST_KEY" in json.dumps(registry.to_dict())
    assert "TEST_KEY" not in repr(source)
    with pytest.raises(CredentialError):
        source.resolve_auth()
    monkeypatch.setenv("TEST_KEY", "PRIVATE_VALUE")
    assert source.resolve_auth() == {"Authorization": "Bearer PRIVATE_VALUE"}
    assert "PRIVATE_VALUE" not in repr(registry)
    assert "PRIVATE_VALUE" not in repr(source)
    assert "PRIVATE_VALUE" not in json.dumps(source.to_dict())
    monkeypatch.setenv("TEST_KEY", "ROTATED")
    assert source.resolve_auth()["Authorization"] == "Bearer ROTATED"


@pytest.mark.parametrize("value", ["", "  ", "leading ", "trailing\n", "a\rb", "a\tb", "unicode-é"])
def test_invalid_credentials(config, monkeypatch, value):
    config["sources"]["local"]["auth"] = {"type": "bearer", "env": "TEST_KEY"}
    monkeypatch.setenv("TEST_KEY", value)
    with pytest.raises(CredentialError):
        Registry.from_dict(config).source().resolve_auth()


def test_custom_header(config, monkeypatch):
    config["sources"]["local"]["auth"] = {"type": "header", "env": "TEST_KEY", "header": "api-key"}
    monkeypatch.setenv("TEST_KEY", "VALUE")
    assert Registry.from_dict(config).source().resolve_auth() == {"api-key": "VALUE"}


def test_no_auth_does_not_borrow_ambient_key(config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "WRONG_KEY")
    assert Registry.from_dict(config).source().resolve_auth() == {}


def test_models_request_and_check_report(config, endpoint, monkeypatch):
    source = config["sources"]["local"]
    source["base_url"] = endpoint["base_url"] + "/"
    source["auth"] = {"type": "bearer", "env": "TEST_KEY"}
    monkeypatch.setenv("TEST_KEY", "RIGHT_KEY")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    connected = Registry.from_dict(config).source()
    assert connected.list_models() == ["test-model", "another-model"]
    request = endpoint["requests"][0]
    assert request["path"] == "/v1/models"
    assert request["headers"]["Authorization"] == "Bearer RIGHT_KEY"
    report = connected.check()
    assert report.ok
    Draft202012Validator(load_schema("check")).validate(report.to_dict())
    assert "inference was not tested" in report.message


def test_probe_failure_is_structured_and_redacted(config, endpoint):
    config["sources"]["local"]["base_url"] = endpoint["base_url"]
    endpoint["status"] = 401
    result = Registry.from_dict(config).source().check()
    assert not result.ok
    assert "401" in result.message
    assert "DO_NOT_PRINT_PROVIDER_BODY" not in json.dumps(result.to_dict())
    assert len(endpoint["requests"]) == 1


def test_ordinary_models_failure_raises(config, endpoint):
    import httpx
    config["sources"]["local"]["base_url"] = endpoint["base_url"]
    endpoint["status"] = 429
    with pytest.raises(httpx.HTTPStatusError):
        Registry.from_dict(config).source().list_models()
    assert len(endpoint["requests"]) == 1


def test_redirect_not_followed(config, endpoint):
    config["sources"]["local"]["base_url"] = endpoint["base_url"]
    endpoint["status"] = 307
    endpoint["location"] = endpoint["base_url"] + "/leak"
    assert not Registry.from_dict(config).source().check().ok
    assert len(endpoint["requests"]) == 1


def test_missing_credentials_failure_has_no_network(config, monkeypatch):
    config["sources"]["local"]["auth"] = {"type": "bearer", "env": "MISSING_KEY"}
    monkeypatch.delenv("MISSING_KEY", raising=False)
    result = Registry.from_dict(config).source().check()
    assert not result.ok
    assert "MISSING_KEY" in result.message


def test_relative_ca_path_uses_config_directory(config, tmp_path, monkeypatch):
    config["sources"]["local"]["ca_bundle"] = "certs/institution.pem"
    seen = []
    sentinel = object()
    def create(*args, **kwargs):
        seen.append(kwargs)
        return sentinel
    monkeypatch.setattr(ssl, "create_default_context", create)
    registry = Registry(config, config_path=tmp_path / "config.toml")
    assert registry.source()._tls_context() is sentinel
    assert seen == [{"cafile": str(tmp_path / "certs/institution.pem")}]


def test_invalid_check_and_model_arguments(config):
    source = Registry.from_dict(config).source()
    with pytest.raises(ValueError):
        source.check(test="invented")
    with pytest.raises(ValueError):
        source.check(model="")


def test_basic_inspection_does_not_import_llm(isolated_config):
    import os
    import subprocess
    import sys
    code = "import sys; from sheetbend import Registry; print(Registry.from_file()); assert 'llm' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", code], env=os.environ.copy(), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
