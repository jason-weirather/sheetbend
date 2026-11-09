"""Credential confidentiality and low-traffic inventory tests."""

from copy import deepcopy
import io
import json
import os
import subprocess
from types import SimpleNamespace

from click.testing import CliRunner
from jsonschema import Draft202012Validator
import pytest
from rich.console import Console

from sheetbend import Dashboard, Registry
from sheetbend.cli import main
from sheetbend.config import load_schema
from sheetbend.credentials import CredentialStore
from sheetbend.dashboard import render_dashboard
from sheetbend.errors import ConfigError, CredentialError


def private_file(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


def file_config(config, path, **kwargs):
    data = deepcopy(config)
    data['credentials'] = {'local-key': {'provider': 'file', 'path': str(path), **kwargs}}
    data['sources']['local']['auth'] = {'type': 'bearer', 'credential': 'local-key'}
    return data


@pytest.mark.parametrize('ending', ['', '\n', '\r\n'])
def test_private_text_files_are_lazy_and_exact(config, tmp_path, ending):
    path = tmp_path / 'secret.txt'
    data = file_config(config, path)
    registry = Registry.from_dict(data)
    assert not path.exists()
    with pytest.raises(CredentialError):
        registry.source().resolve_auth()
    private_file(path, 'SYNTHETIC_KEY' + ending)
    assert registry.source().resolve_auth() == {'Authorization': 'Bearer SYNTHETIC_KEY'}
    assert 'SYNTHETIC_KEY' not in json.dumps(registry.to_dict())
    assert 'SYNTHETIC_KEY' not in repr(registry.credentials)


def test_file_toml_key_and_relative_config_directory(config, tmp_path):
    private_file(tmp_path / 'secrets.toml', 'omlx = "SYNTHETIC_OMLX"\nother = "UNRELATED_SECRET"\n')
    registry = Registry(file_config(config, 'secrets.toml', key='omlx'), config_path=tmp_path / 'config.toml')
    assert registry.source().resolve_auth() == {'Authorization': 'Bearer SYNTHETIC_OMLX'}
    assert 'UNRELATED_SECRET' not in json.dumps(registry.to_dict())


@pytest.mark.parametrize('mode', [0o644, 0o640, 0o664, 0o777])
def test_nonprivate_file_modes_fail_closed(config, tmp_path, mode):
    path = private_file(tmp_path / 'secret.txt', 'SYNTHETIC_KEY')
    path.chmod(mode)
    with pytest.raises(CredentialError, match='owner-only'):
        Registry.from_dict(file_config(config, path)).source().resolve_auth()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'directory'])
def test_special_files_fail_without_blocking(config, tmp_path, kind):
    target = private_file(tmp_path / 'target', 'SYNTHETIC_KEY')
    path = tmp_path / 'key'
    if kind == 'symlink':
        path.symlink_to(target)
    elif kind == 'hardlink':
        os.link(target, path)
    elif kind == 'fifo':
        os.mkfifo(path, mode=0o600)
    else:
        path.mkdir(mode=0o700)
    with pytest.raises(CredentialError):
        Registry.from_dict(file_config(config, path)).source().resolve_auth()


@pytest.mark.parametrize('value', ['', ' ', ' KEY', 'KEY ', 'ONE\nTWO', 'KEY\n\n', 'SYNTHETIC_é', 'x' * 65537])
def test_bad_secret_values_fail_without_echoing(config, tmp_path, value):
    path = private_file(tmp_path / 'key', value)
    with pytest.raises(CredentialError) as caught:
        Registry.from_dict(file_config(config, path)).source().resolve_auth()
    assert 'SYNTHETIC_' not in str(caught.value)


def test_bad_toml_error_never_echoes_values(config, tmp_path):
    path = private_file(tmp_path / 'key', 'key = "SYNTHETIC_PRIVATE_INVALID')
    with pytest.raises(CredentialError) as caught:
        Registry.from_dict(file_config(config, path, key='key')).source().resolve_auth()
    assert 'SYNTHETIC_PRIVATE' not in str(caught.value)


def test_secret_file_is_data_not_shell(config, tmp_path):
    path = private_file(tmp_path / 'key', '$(NEVER_EXECUTE_THIS)')
    assert Registry.from_dict(file_config(config, path)).source().resolve_auth()['Authorization'] == 'Bearer $(NEVER_EXECUTE_THIS)'


def test_named_environment_credential(config, monkeypatch):
    data = deepcopy(config)
    data['credentials'] = {'key': {'provider': 'env', 'name': 'TEST_CREDENTIAL'}}
    data['sources']['local']['auth'] = {'type': 'header', 'header': 'api-key', 'credential': 'key'}
    monkeypatch.setenv('TEST_CREDENTIAL', 'SYNTHETIC_TOKEN')
    assert Registry.from_dict(data).source().resolve_auth() == {'api-key': 'SYNTHETIC_TOKEN'}


def test_keychain_success_is_a_bounded_shell_free_read(monkeypatch, tmp_path):
    import sheetbend.credentials as module
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    seen = []
    def run(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'SYNTHETIC_KEYCHAIN\n', stderr=b'')
    monkeypatch.setattr(module.subprocess, 'run', run)
    store = CredentialStore({'key': {'provider': 'keychain', 'service': 'ai.omlx', 'account': 'test-user'}}, tmp_path)
    assert store.resolve('key') == 'SYNTHETIC_KEYCHAIN'
    command, options = seen[0]
    assert command == ['/usr/bin/security', 'find-generic-password', '-s', 'ai.omlx', '-a', 'test-user', '-w']
    assert options['timeout'] == 15 and options.get('shell', False) is False
    assert 'SYNTHETIC_KEYCHAIN' not in repr(store)


@pytest.mark.parametrize('failure', ['denied', 'timeout', 'unavailable'])
def test_keychain_failures_are_redacted(monkeypatch, tmp_path, failure):
    import sheetbend.credentials as module
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    def run(*args, **kwargs):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('security', 15, output='SYNTHETIC_PRIVATE')
        if failure == 'unavailable':
            raise OSError('SYNTHETIC_PRIVATE')
        return SimpleNamespace(returncode=1, stdout=b'SYNTHETIC_PRIVATE', stderr=b'SYNTHETIC_PRIVATE')
    monkeypatch.setattr(module.subprocess, 'run', run)
    store = CredentialStore({'key': {'provider': 'keychain', 'service': 'ai.omlx', 'account': 'test'}}, tmp_path)
    with pytest.raises(CredentialError) as caught:
        store.resolve('key')
    assert 'SYNTHETIC_PRIVATE' not in str(caught.value)


def test_keychain_on_other_platforms_fails_explicitly(monkeypatch, tmp_path):
    import sheetbend.credentials as module
    monkeypatch.setattr(module.sys, 'platform', 'linux')
    store = CredentialStore({'key': {'provider': 'keychain', 'service': 'a', 'account': 'b'}}, tmp_path)
    with pytest.raises(CredentialError, match='macOS'):
        store.resolve('key')


def test_named_reference_changes_participate_in_bucket_conflict(config, tmp_path):
    data = file_config(config, 'one.txt')
    first = Registry(data, config_path=tmp_path / 'config.toml').source()
    data['credentials']['local-key']['path'] = 'two.txt'
    second = Registry(data, config_path=tmp_path / 'config.toml').source()
    assert first._admission.bucket == second._admission.bucket
    assert first._admission.fingerprint != second._admission.fingerprint


@pytest.mark.parametrize('auth', [
    {'type':'bearer','credential':'missing'},
    {'type':'bearer','env':'KEY','credential':'missing'},
    {'type':'none','credential':'missing'},
])
def test_bad_credential_references_rejected_offline(config, auth):
    config['sources']['local']['auth'] = auth
    with pytest.raises(ConfigError):
        Registry.from_dict(config)


def test_azure_provider_is_not_silently_pretended_to_exist(config):
    config['credentials'] = {'key': {'provider': 'azure', 'vault': 'example'}}
    with pytest.raises(ConfigError):
        Registry.from_dict(config)


def test_dashboard_is_offline_by_default(config, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError('Unexpected credential resolution')
    monkeypatch.setattr(CredentialStore, 'resolve', fail)
    snapshot = Dashboard(Registry.from_dict(config)).snapshot()
    assert len(snapshot['models']) == 2
    assert all(row['catalog_status'] == 'unchecked' for row in snapshot['models'])
    Draft202012Validator(load_schema('dashboard')).validate(snapshot)


def test_dashboard_one_catalog_per_source_and_sixty_second_cache(config, endpoint):
    config['sources']['local']['base_url'] = endpoint['base_url']
    config['sources']['local']['models']['absent-model'] = {}
    dashboard = Dashboard(Registry.from_dict(config))
    assert dashboard.probe()
    assert not dashboard.probe()
    snapshot = dashboard.snapshot()
    Draft202012Validator(load_schema('dashboard')).validate(snapshot)
    assert len(endpoint['requests']) == 1 and endpoint['requests'][0]['method'] == 'GET'
    assert [row['model_status'] for row in snapshot['models']] == ['listed', 'listed', 'absent']
    assert all(row['inference_status'] == 'not_tested' for row in snapshot['models'])


def test_readable_credentials_do_not_imply_endpoint_acceptance(config, endpoint, tmp_path):
    data = file_config(config, private_file(tmp_path / 'key', 'SYNTHETIC_KEY'))
    data['sources']['local']['base_url'] = endpoint['base_url']
    endpoint['status'] = 401
    dashboard = Dashboard(Registry.from_dict(data))
    dashboard.probe()
    snapshot = dashboard.snapshot()
    assert snapshot['credentials'][0]['status'] == 'available'
    assert snapshot['models'][0]['catalog_status'] == 'unavailable'
    assert 'SYNTHETIC_KEY' not in json.dumps(snapshot)


def test_external_sources_are_listed_not_contacted(config, endpoint):
    config['sources']['local'].update(scope='external', base_url=endpoint['base_url'])
    dashboard = Dashboard(Registry.from_dict(config))
    dashboard.probe()
    assert dashboard.snapshot()['models'][0]['catalog_status'] == 'blocked'
    assert endpoint['requests'] == []
    dashboard = Dashboard(Registry.from_dict(config), allow_external=True)
    dashboard.probe()
    assert dashboard.snapshot()['models'][0]['model_status'] == 'listed'


def test_credential_failure_does_not_contact_endpoint(config, endpoint, tmp_path):
    data = file_config(config, tmp_path / 'missing-key')
    data['sources']['local']['base_url'] = endpoint['base_url']
    dashboard = Dashboard(Registry.from_dict(data))
    dashboard.probe()
    assert dashboard.snapshot()['models'][0]['credential_status'] == 'unavailable'
    assert endpoint['requests'] == []


def test_plain_dashboard_and_cli_contract(config, tmp_path):
    config['sources']['local']['models']['model-\u03bb'] = {}
    snapshot = Dashboard(Registry.from_dict(config)).snapshot()
    handle = io.StringIO()
    Console(file=handle, width=160, force_terminal=False, no_color=True).print(render_dashboard(snapshot, plain=True))
    output = handle.getvalue()
    assert output.isascii() and '\x1b' not in output
    assert 'unchecked' in output and 'not a live listener check' in ' '.join(output.split())
    result = CliRunner().invoke(main, ['--config-path', 'examples/config.toml', 'dashboard', '--json'])
    assert result.exit_code == 0, result.output
    Draft202012Validator(load_schema('dashboard')).validate(json.loads(result.output))


def test_credential_probe_needs_no_inference_extra(config, tmp_path):
    data = file_config(config, private_file(tmp_path / 'key', 'SYNTHETIC_KEY'))
    result = Registry.from_dict(data).source().check(test='credentials')
    assert result.ok and 'acceptance was not tested' in result.message
    Draft202012Validator(load_schema('check')).validate(result.to_dict())


def test_new_schemas_are_valid():
    Draft202012Validator.check_schema(load_schema('config'))
    Draft202012Validator.check_schema(load_schema('dashboard'))


def test_gateway_examples_validate_offline():
    for name in ('studio-gateway', 'federated-gateway'):
        registry = Registry.from_file(f'examples/{name}.toml')
        assert registry.to_dict()['gateway']['routes']


@pytest.mark.parametrize('args', [
    ['--watch', '--json'], ['--interval-seconds', 'inf'], ['--interval-seconds', 'nan'],
    ['--test', 'text'], ['--source', 'local'],
])
def test_dashboard_cli_rejects_ambiguous_arguments(args):
    result = CliRunner().invoke(main, ['--config-path', 'examples/config.toml', 'dashboard', *args])
    assert result.exit_code == 2
