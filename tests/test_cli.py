import json

from click.testing import CliRunner
import pytest

from sheetbend import __version__
from sheetbend.cli import main


def test_sources_missing_config_is_offline(isolated_config):
    result = CliRunner().invoke(main, ["sources"])
    assert result.exit_code == 0, result.output
    assert "No sources configured" in result.output
    data = CliRunner().invoke(main, ["sources", "--json"])
    assert json.loads(data.output) == {"schema_version": 2, "sources": {}}


@pytest.mark.parametrize("command", [["version"], ["schema"], ["schema", "--name", "check"]])
def test_metadata_commands_ignore_bad_config(isolated_config, monkeypatch, command):
    path = isolated_config / "invalid.toml"
    path.write_text("broken = [")
    monkeypatch.setenv("SHEETBEND_CONFIG", str(path))
    result = CliRunner().invoke(main, command)
    assert result.exit_code == 0, result.output
    if command == ["version"]:
        assert result.output.strip() == __version__
    else:
        assert "$schema" in json.loads(result.output)


def test_cli_inspection_and_secret_redaction(monkeypatch):
    monkeypatch.setenv("MSK_LLM_API_KEY", "PRIVATE_VALUE")
    args = ["--config-path", "examples/config.toml"]
    runner = CliRunner()
    result = runner.invoke(main, args + ["sources"])
    assert result.exit_code == 0, result.output
    assert "local *" in result.output
    result = runner.invoke(main, args + ["inspect", "msk", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["auth"]["env"] == "MSK_LLM_API_KEY"
    assert "PRIVATE_VALUE" not in result.output


def test_empty_registry_cannot_resolve_source(isolated_config):
    result = CliRunner().invoke(main, ["inspect"])
    assert result.exit_code != 0
    assert "No configured source satisfies" in result.output


def test_check_json_failure_exit_status(isolated_config, monkeypatch):
    path = isolated_config / "config.toml"
    path.write_text("""schema_version = 2
[sources.test]
protocol = "openai-compatible"
base_url = "https://inference.example.org/v1"
default_model = "test"
scope = "external"
auth = {type = "bearer", env = "NEVER_SET_THIS_KEY"}
[sources.test.models.test]
""")
    monkeypatch.delenv("NEVER_SET_THIS_KEY", raising=False)
    result = CliRunner().invoke(main, ["--config-path", str(path), "check", "test", "--allow-external", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["ok"] is False


def test_help_explains_default_and_cost():
    result = CliRunner().invoke(main, ["check", "--help"])
    assert result.exit_code == 0
    assert "default: models" in result.output
    assert "may cost money" in " ".join(result.output.split())
