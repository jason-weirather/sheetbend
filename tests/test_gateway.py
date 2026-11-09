"""Real Uvicorn -> HTTPX -> loopback upstream tests; no LLM test doubles required."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from threading import Event, Thread
import time

import httpx
import pytest

from sheetbend import Registry, Runtime
from sheetbend.errors import ConfigError, SelectionError
from sheetbend.gateway import Gateway, GatewayServer
from sheetbend.launch import child_environment, run_with_gateway

pytest.importorskip('uvicorn', reason='Optional gateway extra is not installed')

CLIENT_A = 'a' * 48
CLIENT_B = 'b' * 48
PROVIDER_KEY = 'SYNTHETIC_UPSTREAM_KEY_DO_NOT_EXPOSE'


@pytest.fixture
def upstream():
    state = {'requests': [], 'status': 200, 'delay': 0, 'stream_delay': 0,
             'done': True, 'invalid': False, 'entered': Event()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.reply(None)

        def do_POST(self):
            self.reply(json.loads(self.rfile.read(int(self.headers['Content-Length']))))

        def reply(self, body):
            state['requests'].append({'path': self.path, 'headers': dict(self.headers), 'body': body})
            state['entered'].set()
            if 'release' in state:
                state['release'].wait(10)
            time.sleep(state['delay'])
            status = state['status']
            if status != 200:
                result = {'error': {'message': PROVIDER_KEY}}
            elif body is None:
                result = {'data': [{'id': 'test-model'}]}
            elif self.path == '/v1/systemone':
                answers = {}
                for name, question in body['questions'].items():
                    kind = question['type']
                    if kind == 'noul':
                        answer = {'type': kind, 'noul': 0.75}
                    else:
                        choices = list(question['criteria']) if kind == 'choice' else [
                            str(i) for i in range(len(question['criteria']))]
                        probabilities = {choice: 1 / len(choices) for choice in choices}
                        answer = {'type': kind, 'probabilities': probabilities, 'confidence': 0.5}
                        answer['choice' if kind == 'choice' else 'score'] = choices[0] if kind == 'choice' else 0.5
                    answers[name] = answer
                result = {'model': body['model'], 'answers': answers,
                          'usage': {'input_tokens': 8, 'output_tokens': 0}}
                if state['invalid']:
                    result['answers'] = {}
            else:
                result = {'object': 'chat.completion', 'id': 'synthetic-id', 'created': 1,
                          'model': body['model'], 'choices': [{'index': 0,
                              'message': {'role': 'assistant', 'content': 'OK', 'reasoning_content': 'trace'},
                              'finish_reason': 'stop'}],
                          'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}}
                if body.get('tools'):
                    result['choices'][0]['message']['tool_calls'] = [
                        {'id': 'call-test', 'type': 'function',
                         'function': {'name': 'test_tool', 'arguments': '{"ok":true}'}}]
            self.send_response(status)
            self.send_header('X-Secret-Upstream', PROVIDER_KEY)
            self.send_header('Location', 'https://example.invalid/' + PROVIDER_KEY)
            try:
                if status == 200 and body and body.get('stream'):
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    for i in range(4):
                        chunk = {'model': body['model'], 'choices': [{'index': 0,
                                 'delta': {'content': 'ok', 'reasoning_content': 'trace'},
                                 'finish_reason': None}]}
                        if i == 3:
                            chunk['usage'] = result['usage']
                        data = ('data: ' + json.dumps(chunk) + '\r\n\r\n').encode()
                        # Split an event across transport writes.
                        self.wfile.write(data[:7])
                        self.wfile.flush()
                        self.wfile.write(data[7:])
                        self.wfile.flush()
                        time.sleep(state['stream_delay'])
                    if state['done']:
                        self.wfile.write(b'data: [DONE]\n\n')
                        self.wfile.flush()
                else:
                    data = json.dumps(result).encode()
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    state['base_url'] = f'http://127.0.0.1:{server.server_port}/v1'
    try:
        yield state
    finally:
        if 'release' in state:
            state['release'].set()
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.fixture
def gateway_config(config, upstream, monkeypatch):
    data = deepcopy(config)
    data['sources']['local']['base_url'] = upstream['base_url']
    data['sources']['local']['auth'] = {'type': 'bearer', 'credential': 'provider'}
    data['sources']['local']['models']['test-model'] = {'capabilities': {
        'tools': True, 'vision': True, 'json_schema': True, 'system_one': True}}
    data['credentials'] = {
        'provider': {'provider': 'env', 'name': 'PROVIDER_KEY'},
        'alice': {'provider': 'env', 'name': 'CLIENT_A'},
        'bob': {'provider': 'env', 'name': 'CLIENT_B'},
    }
    data['gateway'] = {
        'routes': {'chat': {'source': 'local', 'model': 'test-model'},
                   'other': {'source': 'local', 'model': 'another-model'}},
        'clients': {'alice': {'credential': 'alice', 'routes': ['chat']},
                    'bob': {'credential': 'bob', 'routes': ['other']}},
    }
    monkeypatch.setenv('PROVIDER_KEY', PROVIDER_KEY)
    monkeypatch.setenv('CLIENT_A', CLIENT_A)
    monkeypatch.setenv('CLIENT_B', CLIENT_B)
    return data


def auth(token=CLIENT_A):
    return {'Authorization': f'Bearer {token}'}


def prompt(**kwargs):
    return {'model': 'chat', 'messages': [{'role': 'user', 'content': 'Hello'}], **kwargs}


def post(server, body=None, path='/chat/completions', **kwargs):
    return httpx.post(server.base_url + path, json=body or prompt(), headers=auth(),
                      trust_env=False, timeout=5, **kwargs)


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('Condition did not become true before deadline')


def active():
    return [r for r in Runtime().snapshot()['requests'] if r['state'] in {'waiting', 'running', 'streaming'}]


def test_catalog_is_static_authenticated_and_route_filtered(gateway_config, upstream):
    app = Gateway.configured(Registry.from_dict(gateway_config))
    assert PROVIDER_KEY not in repr(app) and CLIENT_A not in repr(app)
    with GatewayServer(app, port=0) as server:
        for headers, expected in [({}, 401), (auth('wrong'), 401), (auth(), 200), (auth(CLIENT_B), 200)]:
            response = httpx.get(server.base_url + '/models', headers=headers, trust_env=False)
            assert response.status_code == expected
            if expected == 200:
                assert [r['id'] for r in response.json()['data']] == (['other'] if headers == auth(CLIENT_B) else ['chat'])
    assert upstream['requests'] == []
    assert not active()


def test_forwarding_rewrites_model_and_keeps_provider_key_private(gateway_config, upstream):
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        response = httpx.post(server.base_url + '/chat/completions', json=prompt(),
                             headers={**auth(), 'api-key': 'BAD_CLIENT_KEY', 'Cookie': 'BAD_COOKIE'}, trust_env=False)
        assert response.status_code == 200
        assert response.json()['model'] == 'chat'
        assert response.json()['choices'][0]['message']['reasoning_content'] == 'trace'
        assert PROVIDER_KEY not in response.text and PROVIDER_KEY not in str(response.headers)
    incoming = upstream['requests'][0]
    assert incoming['body']['model'] == 'test-model'
    assert incoming['headers']['Authorization'] == 'Bearer ' + PROVIDER_KEY
    assert 'api-key' not in incoming['headers'] and 'Cookie' not in incoming['headers']
    assert CLIENT_A not in json.dumps(incoming)
    row = Runtime().snapshot()['requests'][0]
    assert row['application'] == 'gateway:alice' and row['state'] == 'done'
    assert (row['input_tokens'], row['output_tokens']) == (3, 2)
    assert PROVIDER_KEY not in json.dumps(Runtime().snapshot())


@pytest.mark.parametrize('status', [302, 400, 401, 429, 500])
def test_errors_and_redirects_are_redacted(gateway_config, upstream, status, capsys):
    upstream['status'] = status
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        response = post(server)
        assert response.status_code == (502 if status == 302 else status)
        assert PROVIDER_KEY not in response.text
        assert 'location' not in response.headers
    assert len(upstream['requests']) == 1
    assert Runtime().snapshot()['requests'][0]['state'] == 'error'
    assert PROVIDER_KEY not in str(capsys.readouterr())


@pytest.mark.parametrize('body', [prompt(model='other'), prompt(model='not-configured')])
def test_token_cannot_choose_another_route(gateway_config, upstream, body):
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        assert post(server, body).status_code == 403
    assert upstream['requests'] == []


@pytest.mark.parametrize('body', [
    prompt(base_url='https://example.invalid'), prompt(extra_body={'model': 'other'}),
    prompt(api_key='SYNTHETIC_WRONG'), prompt(stream='yes'), prompt(messages=[]),
    prompt(chat_template_kwargs={'arbitrary': True}), prompt(reasoning_effort='max'),
    prompt(messages=[{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'http://127.0.0.1:1'}}]}]),
])
def test_unsupported_requests_fail_before_network(gateway_config, upstream, body):
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        assert post(server, body).status_code == 400
    assert upstream['requests'] == []
    assert not Runtime().snapshot()['requests']


def test_unknown_routes_origin_and_bad_host_are_not_forwarded(gateway_config, upstream):
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        assert post(server, path='/responses').status_code == 404
        for headers, expected in [({**auth(), 'Origin': 'https://example.invalid'}, 403),
                                  ({**auth(), 'Host': 'example.invalid'}, 400)]:
            response = httpx.get(server.base_url + '/models', headers=headers, trust_env=False)
            assert response.status_code == expected
    assert upstream['requests'] == []


@pytest.mark.parametrize('raw', [b'{"model":"chat","model":"other"}', b'{"model":NaN}', b'{"temperature":1e999}', b'not json'])
def test_ambiguous_or_invalid_json_is_rejected(gateway_config, upstream, raw):
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        response = httpx.post(server.base_url + '/chat/completions', content=raw,
                             headers={**auth(), 'Content-Type': 'application/json'}, trust_env=False)
        assert response.status_code == 400
    assert upstream['requests'] == []


def test_request_body_limit(gateway_config, upstream):
    gateway_config['gateway']['max_body_bytes'] = 1024
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        assert post(server, prompt(messages=[{'role': 'user', 'content': 'x' * 2000}])).status_code == 413
    assert upstream['requests'] == []


def test_bound_reasoning_rejects_conflicts_but_allows_identical_federation_values(gateway_config, upstream):
    gateway_config['sources']['local']['models']['test-model']['reasoning'] = {
        'control': 'chat_template_kwargs', 'default': 'off'}
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        assert post(server, prompt(chat_template_kwargs={'enable_thinking': True})).status_code == 400
        assert post(server, prompt(chat_template_kwargs={'enable_thinking': False})).status_code == 200
    assert upstream['requests'][0]['body']['chat_template_kwargs'] == {'enable_thinking': False}


def test_streaming_rewrites_alias_and_tracks_usage(gateway_config, upstream):
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        response = post(server, prompt(stream=True))
        assert response.status_code == 200 and 'data: [DONE]' in response.text
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith('data: {')]
        assert len(events) == 4 and all(event['model'] == 'chat' for event in events)
        assert all(event['choices'][0]['delta']['reasoning_content'] == 'trace' for event in events)
    row = Runtime().snapshot()['requests'][0]
    assert row['state'] == 'done' and row['output_tokens'] == 2


def test_truncated_stream_is_error_not_success(gateway_config, upstream):
    upstream['done'] = False
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        response = post(server, prompt(stream=True))
        assert 'upstream_error' in response.text and '[DONE]' not in response.text
    assert Runtime().snapshot()['requests'][0]['state'] == 'error'


def test_client_disconnect_releases_stream_admission(gateway_config, upstream):
    upstream['stream_delay'] = 0.2
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        with httpx.stream('POST', server.base_url + '/chat/completions', json=prompt(stream=True),
                          headers=auth(), trust_env=False) as response:
            assert next(response.iter_lines()).startswith('data:')
        wait_for(lambda: not active())
    assert Runtime().snapshot()['requests'][0]['state'] == 'cancelled'


def test_queue_deadline_shares_python_source_budget(gateway_config, upstream):
    gateway_config['gateway']['queue_timeout_seconds'] = 0.15
    gateway_config['sources']['local']['rate_limit'] = {'max_concurrent': 1}
    registry = Registry.from_dict(gateway_config)
    source = registry.source()
    with GatewayServer(Gateway.configured(registry), port=0) as server:
        with source._admission.acquire(model='test-model', operation='generate'):
            assert post(server).status_code == 503
            assert upstream['requests'] == []
            assert len(active()) == 1
        assert post(server).status_code == 200
    assert not active()


def test_disconnect_while_queued_does_not_leak_slot(gateway_config, upstream):
    gateway_config['sources']['local']['rate_limit'] = {'max_concurrent': 1}
    registry = Registry.from_dict(gateway_config)
    with GatewayServer(Gateway.configured(registry), port=0) as server:
        with registry.source()._admission.acquire(model='test-model', operation='generate'):
            port = int(server.base_url.split(':')[2].split('/')[0])
            sock = socket.create_connection(('127.0.0.1', port))
            body = json.dumps(prompt()).encode()
            sock.sendall((f'POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
                          f'Authorization: Bearer {CLIENT_A}\r\nContent-Type: application/json\r\n'
                          f'Content-Length: {len(body)}\r\n\r\n').encode() + body)
            wait_for(lambda: len(active()) == 2)
            sock.close()
            wait_for(lambda: len(active()) == 1)
        assert post(server).status_code == 200
    assert not active()


def test_upstream_timeout_is_bounded_and_accounted(gateway_config, upstream):
    upstream['delay'] = 0.5
    gateway_config['gateway']['request_timeout_seconds'] = 0.2
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        assert post(server).status_code == 504
        wait_for(lambda: not active())


def test_concurrent_requests_share_a_source_limit(gateway_config, upstream):
    upstream['release'] = Event()
    gateway_config['sources']['local']['rate_limit'] = {'max_concurrent': 1}
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(post, server)
            assert upstream['entered'].wait(3)
            second = pool.submit(post, server)
            try:
                wait_for(lambda: len(active()) == 2, timeout=3)
                assert sum(row['state'] == 'running' for row in active()) == 1
                assert len(upstream['requests']) == 1
            finally:
                upstream['release'].set()
            assert first.result().status_code == second.result().status_code == 200
    assert not active()


def test_system_one_native_gateway_and_python(gateway_config, upstream):
    questions = {'ready': {'type': 'noul', 'instructions': 'The status is READY.'},
                 'choice': {'type': 'choice', 'instructions': 'Pick a state.', 'criteria': {'ready': 'Ready', 'busy': 'Busy'}},
                 'score': {'type': 'score', 'instructions': 'Rate urgency.', 'criteria': ['Low', 'High']}}
    registry = Registry.from_dict(gateway_config)
    with GatewayServer(Gateway.configured(registry), port=0) as server:
        response = post(server, {'model': 'chat', 'state': 'READY', 'questions': questions}, path='/systemone')
        assert response.status_code == 200 and response.json()['answers']['ready']['noul'] == 0.75
    result = registry.source().decide('READY', questions)
    assert result['answers']['choice']['choice'] == 'ready'
    assert all(r['operation'] == 'system_one' for r in Runtime().snapshot()['requests'])
    assert all(r['output_tokens'] == 0 for r in Runtime().snapshot()['requests'])
    assert all(r['path'] == '/v1/systemone' for r in upstream['requests'])


def test_system_one_capability_is_independent_of_chat_and_reasoning(gateway_config, upstream):
    model = gateway_config['sources']['local']['models']['test-model']
    model['capabilities']['chat'] = False
    registry = Registry.from_dict(gateway_config)
    with pytest.raises(SelectionError), registry.source().connect():
        pass
    with GatewayServer(Gateway.configured(registry), port=0) as server:
        assert post(server).status_code == 400
        assert post(server, {'model': 'chat', 'state': 'READY', 'questions': {
            'ready': {'type': 'noul', 'instructions': 'Ready?'}}}, path='/systemone').status_code == 200
    assert registry.source().check(test='system_one').ok
    assert [r.test for r in registry.source().check_all().results] == ['models', 'system_one']


def test_invalid_system_one_answer_fails_closed(gateway_config, upstream):
    upstream['invalid'] = True
    registry = Registry.from_dict(gateway_config)
    assert not registry.source().check(test='system_one').ok
    with GatewayServer(Gateway.configured(registry), port=0) as server:
        assert post(server, {'model': 'chat', 'state': 'READY', 'questions': {
            'ready': {'type': 'noul', 'instructions': 'Ready?'}}}, path='/systemone').status_code == 502


def test_federation_through_two_authenticated_gateways(gateway_config, upstream, monkeypatch):
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as inner:
        outer_config = deepcopy(gateway_config)
        outer_config['sources']['local'].update(base_url=inner.base_url, scope='external',
                                               default_model='chat', models={'chat': {'capabilities': {'system_one': True}}},
                                               auth={'type': 'bearer', 'credential': 'alice'})
        outer_config['gateway'] = {'allowed_scopes': ['external'],
            'routes': {'combined': {'source': 'local', 'model': 'chat'}},
            'clients': {'outer': {'credential': 'bob', 'routes': ['combined']}}}
        with GatewayServer(Gateway.configured(Registry.from_dict(outer_config)), port=0) as outer:
            response = httpx.post(outer.base_url + '/chat/completions', json=prompt(model='combined'),
                                  headers=auth(CLIENT_B), trust_env=False)
            assert response.status_code == 200 and response.json()['model'] == 'combined'
            result = httpx.post(outer.base_url + '/systemone', json={
                'model': 'combined', 'state': 'Ready', 'questions': {'q': {'type': 'noul', 'instructions': 'Ready?'}}},
                headers=auth(CLIENT_B), trust_env=False)
            assert result.status_code == 200 and result.json()['model'] == 'combined'
    assert upstream['requests'][0]['headers']['X-Sheetbend-Hop'] == '2'
    assert upstream['requests'][0]['headers']['Authorization'] == 'Bearer ' + PROVIDER_KEY
    assert len(Runtime().snapshot()['requests']) == 4


def test_hop_limit(gateway_config, upstream):
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        response = httpx.post(server.base_url + '/chat/completions', json=prompt(),
                             headers={**auth(), 'X-Sheetbend-Hop': '4'}, trust_env=False)
        assert response.status_code == 508
    assert upstream['requests'] == []


def test_run_injects_session_not_upstream_key_and_preserves_exit(gateway_config, upstream, tmp_path, monkeypatch):
    monkeypatch.setenv('UNRELATED_SECRET', 'SYNTHETIC_UNRELATED')
    monkeypatch.setenv('EXAMPLE_FLAG', 'kept')
    output = tmp_path / 'child.json'
    script = '''import os, sys, json, httpx
url = os.environ['OPENAI_BASE_URL']
token = os.environ['OPENAI_API_KEY']
r = httpx.post(url + '/chat/completions', headers={'Authorization': 'Bearer ' + token},
               json={'model': os.environ['SHEETBEND_MODEL'], 'messages':[{'role':'user','content':'Hello'}]}, trust_env=False)
assert r.status_code == 200, r.text
with open(sys.argv[1], 'w') as f:
    json.dump({'url':url, 'token':token, 'keys':list(os.environ), 'flag':os.environ['EXAMPLE_FLAG']}, f)
sys.exit(7)
'''
    before = dict(os.environ)
    result = run_with_gateway(Registry.from_dict(gateway_config).source(),
                             [sys.executable, '-c', script, str(output)], inherit_env=['EXAMPLE_FLAG'],
                             application='test-child')
    assert result == 7 and dict(os.environ) == before
    child = json.loads(output.read_text())
    assert child['token'] not in (CLIENT_A, CLIENT_B, PROVIDER_KEY)
    assert not {'PROVIDER_KEY', 'CLIENT_A', 'CLIENT_B', 'UNRELATED_SECRET'} & set(child['keys'])
    assert child['flag'] == 'kept'
    with pytest.raises(httpx.ConnectError):
        httpx.get(child['url'] + '/models', timeout=1, trust_env=False)
    assert Runtime().snapshot()['requests'][0]['application'] == 'test-child'


def test_environment_adapter_does_not_mutate_parent(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'old')
    monkeypatch.setenv('HTTP_PROXY', 'http://example.invalid')
    env = child_environment('http://127.0.0.1:123/v1', CLIENT_A, 'openai')
    assert env['OPENAI_API_KEY'] == CLIENT_A and os.environ['OPENAI_API_KEY'] == 'old'
    assert 'HTTP_PROXY' not in env
    assert env['SHEETBEND_MODEL'] == 'selected'
    assert 'OPENAI_API_KEY' not in child_environment('http://127.0.0.1:123/v1', CLIENT_A, 'sheetbend')


def test_sigterm_is_forwarded_and_exit_is_preserved(upstream, tmp_path):
    config_file = tmp_path / 'config.toml'
    config_file.write_text(f'''schema_version = 2
[sources.local]
protocol = "openai-compatible"
base_url = "{upstream['base_url']}"
default_model = "test-model"
scope = "local"
auth = {{type="none"}}
[sources.local.models.test-model]
''')
    ready = tmp_path / 'ready.json'
    script = '''import os,sys,json,time
with open(sys.argv[1], 'w') as f: json.dump({'url':os.environ['OPENAI_BASE_URL']},f)
time.sleep(30)
'''
    env = dict(os.environ)
    env['PYTHONPATH'] = str(Path(__file__).parents[1] / 'src')
    process = subprocess.Popen([sys.executable, '-m', 'sheetbend', '--config-path', str(config_file),
                                'run', '--source', 'local', '--', sys.executable, '-c', script, str(ready)],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_for(ready.exists)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 143
        with pytest.raises(httpx.ConnectError):
            httpx.get(json.loads(ready.read_text())['url'] + '/models', trust_env=False, timeout=1)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize('kind', ['missing-source', 'missing-model', 'missing-credential',
                                  'missing-route', 'scope', 'reasoning', 'ambiguous-auth'])
def test_gateway_configuration_rejects_bad_references_offline(gateway_config, kind):
    if kind == 'missing-source':
        gateway_config['gateway']['routes']['chat']['source'] = 'absent'
    elif kind == 'missing-model':
        gateway_config['gateway']['routes']['chat']['model'] = 'absent'
    elif kind == 'missing-credential':
        gateway_config['gateway']['clients']['alice']['credential'] = 'absent'
    elif kind == 'missing-route':
        gateway_config['gateway']['clients']['alice']['routes'] = ['absent']
    elif kind == 'scope':
        gateway_config['sources']['local']['scope'] = 'external'
    elif kind == 'reasoning':
        gateway_config['gateway']['routes']['chat']['reasoning'] = 'off'
    else:
        gateway_config['sources']['local']['auth']['env'] = 'ALSO_SET'
    with pytest.raises(ConfigError):
        Registry.from_dict(gateway_config)


@pytest.mark.parametrize('kind', ['empty', 'weak', 'duplicate'])
def test_gateway_tokens_must_be_explicit_and_distinct(gateway_config, monkeypatch, kind):
    if kind == 'empty':
        gateway_config['gateway']['clients'] = {}
    elif kind == 'weak':
        monkeypatch.setenv('CLIENT_A', 'weak')
    else:
        monkeypatch.setenv('CLIENT_B', CLIENT_A)
    with pytest.raises(ConfigError):
        Gateway.configured(Registry.from_dict(gateway_config))


@pytest.mark.parametrize('source_auth, header, value', [
    ({'type': 'none'}, None, None),
    ({'type': 'bearer-placeholder'}, 'Authorization', 'Bearer sheetbend'),
    ({'type': 'header', 'header': 'api-key', 'credential': 'provider'}, 'api-key', PROVIDER_KEY),
])
def test_gateway_auth_transport_forms(gateway_config, upstream, source_auth, header, value):
    gateway_config['sources']['local']['auth'] = source_auth
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        assert post(server).status_code == 200
    seen = upstream['requests'][0]['headers']
    if header:
        assert seen[header] == value
    if header != 'Authorization':
        assert 'Authorization' not in seen


def test_tools_schema_and_inline_vision_are_preserved(gateway_config, upstream):
    body = prompt(
        messages=[{'role': 'system', 'content': 'Test'}, {'role': 'user', 'content': [
            {'type': 'text', 'text': 'Look'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}]}],
        tools=[{'type': 'function', 'function': {'name': 'test_tool', 'parameters': {'type': 'object'}}}],
        response_format={'type': 'json_schema', 'json_schema': {'name': 'test', 'schema': {'type': 'object'}}},
    )
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        result = post(server, body)
        assert result.status_code == 200 and result.json()['choices'][0]['message']['tool_calls']
    assert upstream['requests'][0]['body']['tools'] == body['tools']
    assert upstream['requests'][0]['body']['response_format'] == body['response_format']
    assert upstream['requests'][0]['body']['messages'] == body['messages']


@pytest.mark.parametrize('capability, payload', [
    ('tools', prompt(tools=[{'type': 'function', 'function': {'name': 'tool'}}])),
    ('streaming', prompt(stream=True)),
    ('json_schema', prompt(response_format={'type': 'json_schema', 'json_schema': {'schema': {}}})),
    ('vision', prompt(messages=[{'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}]}])),
    ('system_prompt', prompt(messages=[{'role': 'system', 'content': 'Test'}])),
])
def test_declared_capabilities_are_enforced(gateway_config, upstream, capability, payload):
    gateway_config['sources']['local']['models']['test-model']['capabilities'][capability] = False
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        assert post(server, payload).status_code == 400
    assert upstream['requests'] == []


def test_actual_routing_cycle_is_bounded(gateway_config):
    # Reserve an unused local port, then configure a deliberate self-cycle.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    gateway_config['sources']['local'].update(
        base_url=f'http://127.0.0.1:{port}/v1', default_model='chat', models={'chat': {}},
        auth={'type': 'bearer', 'credential': 'alice'})
    gateway_config['gateway']['routes'] = {'chat': {'source': 'local', 'model': 'chat'}}
    gateway_config['gateway']['clients'].pop('bob')
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=port) as server:
        response = post(server)
        assert response.status_code == 508
        wait_for(lambda: not active())
    assert len(Runtime().snapshot()['requests']) == 4


def test_dashboard_one_shot_system_one_is_not_repeated_by_probe(gateway_config, upstream):
    from sheetbend import Dashboard
    dashboard = Dashboard(Registry.from_dict(gateway_config))
    assert dashboard.test('local', test='system_one').ok
    assert dashboard.probe()
    assert not dashboard.probe()
    snapshot = dashboard.snapshot()
    from jsonschema import Draft202012Validator
    from sheetbend.config import load_schema
    Draft202012Validator(load_schema('dashboard')).validate(snapshot)
    first = next(row for row in snapshot['models'] if row['model'] == 'test-model')
    assert first['inference_status'] == 'passed' and first['inference_test'] == 'system_one'
    assert sum(row['path'].endswith('/systemone') for row in upstream['requests']) == 1


def test_server_socket_is_loopback_only(gateway_config):
    with GatewayServer(Gateway.configured(Registry.from_dict(gateway_config)), port=0) as server:
        assert server._socket.getsockname()[0] == '127.0.0.1'
        assert server.is_alive
