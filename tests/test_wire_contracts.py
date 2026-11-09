"""Strict JSON/decision transport validation, independent of serving dependencies."""
import json

import pytest

from sheetbend import Registry
from sheetbend.errors import ConfigError, ProbeError, SelectionError
from sheetbend.wire import json_object, prepare_chat, validate_answers


@pytest.mark.parametrize('data', [
    b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b'[]',
    '{"x": "\\ud800"}', '{"x":1}'.encode('utf-16'),
])
def test_strict_utf8_finite_json(data):
    with pytest.raises(ProbeError):
        json_object(data)


@pytest.mark.parametrize('value', [10**400, float('inf'), float('nan'), True, -1, 1.1])
def test_invalid_probability_is_a_probe_error(value):
    request = {'questions': {'q': {'type': 'noul'}}}
    with pytest.raises(ProbeError):
        validate_answers(request, {'answers': {'q': {'type': 'noul', 'noul': value}}})


@pytest.mark.parametrize('extra', [
    {'tools': [{'type': 'web_search'}]},
    {'tools': [{'type': 'mcp', 'server_url': 'https://example.invalid'}]},
    {'tools': [{'type': 'function', 'function': {'name': 'test', 'server_url': 'untrusted'}}]},
    {'tool_choice': {'type': 'web_search'}},
    {'tool_choice': {'type': 'function', 'function': {'name': 5}}},
])
def test_gateway_only_allows_function_tools(config, extra):
    config['sources']['local']['models']['test-model']['capabilities'] = {'tools': True}
    source = Registry.from_dict(config).source()
    body = {'model': 'test-model', 'messages': [{'role': 'user', 'content': 'Hi'}], **extra}
    with pytest.raises(SelectionError):
        prepare_chat(body, source.to_dict()['models']['test-model'], source.resolve_reasoning())


def test_gateway_client_label_validates_offline(config):
    config['credentials'] = {'key': {'provider': 'env', 'name': 'NEVER_RESOLVED'}}
    config['gateway'] = {'routes': {'chat': {'source': 'local', 'model': 'test-model'}},
                         'clients': {'x' * 73: {'credential': 'key', 'routes': ['chat']}}}
    with pytest.raises(ConfigError):
        Registry.from_dict(config)
    config['gateway']['clients'] = {'x' * 72: {'credential': 'key', 'routes': ['chat']}}
    assert json.dumps(Registry.from_dict(config).to_dict())


@pytest.mark.parametrize('args', [
    ['--inherit-env', 'BAD=NAME'], ['--application', 'x' * 81],
])
def test_run_rejects_bad_cli_metadata_before_config(args):
    from click.testing import CliRunner
    from sheetbend.cli import main
    result = CliRunner().invoke(main, ['--config-path', '/missing/config.toml', 'run', *args, '--', 'true'])
    assert result.exit_code == 2
    assert 'configuration' not in result.output.lower()
