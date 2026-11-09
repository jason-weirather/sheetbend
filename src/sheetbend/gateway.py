"""Authenticated, explicitly routed ASGI gateway. No arbitrary upstream URLs or admin proxy.

The HTTP server is optional; inference forwarding uses the same Source admission
ledger as Python connections. Upstream credentials never become client credentials.
"""

import asyncio
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import secrets
import socket
from threading import Event, Thread
import time
from typing import Any

import httpx

from .config import _apply_defaults, load_schema
from .credentials import header_secret
from .errors import ConfigError, CoordinationError, DependencyError, ProbeError, SelectionError
from .reasoning import ReasoningPlan
from .registry import Registry
from .runtime.process import caller_labels
from .source import Source
from .wire import json_object, prepare_chat, prepare_system_one, token_usage, validate_answers


class _HTTPError(Exception):
    def __init__(self, status: int, message: str, code: str = 'request_rejected') -> None:
        self.status, self.message, self.code = status, message, code


class _Disconnected(Exception):
    pass


@dataclass(frozen=True)
class _Target:
    source: Source
    model: str
    definition: dict[str, Any]
    reasoning: ReasoningPlan


@dataclass(frozen=True)
class _Grant:
    digest: bytes = field(repr=False)
    routes: frozenset[str]
    application: str


def _settings(values: dict[str, Any] | None = None) -> dict[str, Any]:
    schema = load_schema()
    result = deepcopy(values or {})
    _apply_defaults(result, schema['$defs']['gateway'], schema)
    return result


def _target(source: Source, model: str | None, reasoning: str | None) -> _Target:
    selected = source._selected_model(model)
    return _Target(source, selected, source._model_definition(selected),
                   source.resolve_reasoning(reasoning, model=selected))


def _grant(token: str, routes: Any, application: str) -> _Grant:
    header_secret(token)
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ConfigError('Gateway client tokens must contain at least 32 non-whitespace characters.')
    caller_labels(application, None)
    return _Grant(hashlib.sha256(token.encode('ascii')).digest(), frozenset(routes), application)


class Gateway:
    """ASGI application. Use configured() or temporary(); credentials resolve at construction.

    Not a sandbox: a same-user process can still read files it independently has
    permission to read. All credentials and config are startup snapshots.
    """

    def __init__(self, targets: dict[str, _Target], grants: list[_Grant],
                 settings: dict[str, Any]) -> None:
        if not targets or not grants:
            raise ConfigError('A gateway requires explicit routes and authenticated clients.')
        if len({grant.digest for grant in grants}) != len(grants):
            raise ConfigError('Gateway clients must use distinct tokens.')
        if any(grant.routes - targets.keys() for grant in grants):
            raise ConfigError('Gateway client refers to an undefined route.')
        self._targets = targets
        self._grants = grants
        self.settings = _settings(settings)
        self._sources = {target.source.name: target.source for target in targets.values()}
        # These maps are private and never included in repr, diagnostics, or catalogs.
        self._headers = {name: source.resolve_auth() for name, source in self._sources.items()}
        self._tls = {name: source._tls_context() for name, source in self._sources.items()}
        self._clients: dict[str, httpx.AsyncClient] = {}

    @classmethod
    def configured(cls, registry: Registry) -> 'Gateway':
        settings = registry.to_dict().get('gateway')
        if settings is None:
            raise ConfigError('Configure [gateway] and explicit routes/clients before serving.')
        targets = {}
        for alias, route in settings['routes'].items():
            source = registry.source(route['source'], allowed_scopes=settings['allowed_scopes'])
            targets[alias] = _target(source, route['model'], route.get('reasoning'))
        grants = [_grant(registry.credentials.resolve(client['credential']), client['routes'],
                         f'gateway:{name}') for name, client in settings['clients'].items()]
        return cls(targets, grants, settings)

    @classmethod
    def temporary(cls, source: Source, *, model: str | None = None,
                  reasoning: str | None = None, application: str = 'sheetbend-run') -> tuple['Gateway', str]:
        """Return an app plus its secret, single-route, lifetime-scoped client token."""
        target = _target(source, model, reasoning)
        token = secrets.token_urlsafe(32)
        return cls({'selected': target}, [_grant(token, ['selected'], application)], {}), token

    def __repr__(self) -> str:
        return f'Gateway(routes={len(self._targets)}, clients={len(self._grants)}, credentials=redacted)'

    async def _lifespan(self, receive: Any, send: Any) -> None:
        while True:
            event = await receive()
            if event['type'] == 'lifespan.startup':
                try:
                    async def strip_cookies(request: httpx.Request) -> None:
                        request.headers.pop("cookie", None)

                    self._clients = {
                        name: httpx.AsyncClient(
                            headers=self._headers[name], verify=self._tls[name], trust_env=False,
                            follow_redirects=False, timeout=source.to_dict()['timeout_seconds'],
                            limits=httpx.Limits(max_connections=self.settings['max_connections']),
                            event_hooks={"request": [strip_cookies]},
                        ) for name, source in self._sources.items()
                    }
                    await send({'type': 'lifespan.startup.complete'})
                except Exception:
                    for client in self._clients.values():
                        await client.aclose()
                    await send({'type': 'lifespan.startup.failed', 'message': 'Gateway startup failed.'})
                    return
            elif event['type'] == 'lifespan.shutdown':
                for client in self._clients.values():
                    await client.aclose()
                self._clients.clear()
                await send({'type': 'lifespan.shutdown.complete'})
                return

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope['type'] == 'lifespan':
            await self._lifespan(receive, send)
            return
        if scope['type'] != 'http':
            return
        started = False

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
            await send(message)

        error = None
        try:
            async with asyncio.timeout(self.settings['request_timeout_seconds']):
                await self._http(scope, receive, tracked_send)
        except _Disconnected:
            return
        except asyncio.CancelledError:
            raise
        except _HTTPError as exc:
            error = exc
        except SelectionError:
            error = _HTTPError(400, 'Request conflicts with the selected model contract.')
        except CoordinationError:
            error = _HTTPError(503, 'Request admission is unavailable.', 'admission_unavailable')
        except (TimeoutError, httpx.TimeoutException):
            error = _HTTPError(504, 'Gateway or upstream deadline exceeded.', 'timeout')
        except (httpx.HTTPError, ProbeError):
            error = _HTTPError(502, 'Invalid or unavailable upstream response.', 'upstream_error')
        except OSError:
            return  # A disconnected downstream socket cannot receive an error body.
        except Exception:
            error = _HTTPError(500, 'Gateway request failed; details withheld.', 'gateway_error')
        if error is not None:
            payload = {'error': {'message': error.message, 'type': error.code}}
            with suppress(OSError, _Disconnected):
                if started:
                    data = b'data: ' + json.dumps(payload).encode() + b'\n\n'
                    await tracked_send({'type': 'http.response.body', 'body': data, 'more_body': False})
                else:
                    await self._json(tracked_send, error.status, payload)

    def _authenticate(self, headers: list[tuple[bytes, bytes]]) -> _Grant:
        authorization = [v for k, v in headers if k.lower() == b'authorization']
        if len(authorization) != 1 or not authorization[0].startswith(b'Bearer '):
            raise _HTTPError(401, 'A valid gateway bearer token is required.', 'authentication_error')
        digest = hashlib.sha256(authorization[0][7:]).digest()
        for grant in self._grants:
            if hmac.compare_digest(digest, grant.digest):
                return grant
        raise _HTTPError(401, 'A valid gateway bearer token is required.', 'authentication_error')

    async def _http(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        headers = scope.get('headers', [])
        # Browser access and ambient proxy metadata are not part of this server-to-server API.
        if any(k.lower() == b'origin' for k, _ in headers):
            raise _HTTPError(403, 'Browser-origin requests are not supported.')
        hosts = [v.split(b':', 1)[0].lower() for k, v in headers if k.lower() == b'host']
        if len(hosts) != 1 or hosts[0] not in {b'127.0.0.1', b'localhost'}:
            raise _HTTPError(400, 'Use a loopback Host header; configure the trusted reverse proxy accordingly.')
        grant = self._authenticate(headers)
        if scope.get('query_string'):
            raise _HTTPError(400, 'Query parameters are not supported.')
        path, method = scope['path'], scope['method']
        if path == '/v1/models' and method == 'GET':
            await self._json(send, 200, {'object': 'list', 'data': [
                {'id': alias, 'object': 'model', 'created': 0, 'owned_by': 'sheetbend',
                 'capabilities': target.definition['capabilities']}
                for alias, target in self._targets.items() if alias in grant.routes
            ]})
            return
        if method != 'POST' or path not in {'/v1/chat/completions', '/v1/systemone'}:
            raise _HTTPError(404, 'Endpoint is not exposed by this gateway.', 'not_found')
        header_map = dict(headers)
        if header_map.get(b'content-type', b'').split(b';')[0].lower() != b'application/json':
            raise _HTTPError(415, 'Use application/json.')
        if header_map.get(b'content-encoding', b'identity').lower() != b'identity':
            raise _HTTPError(415, 'Compressed request bodies are not supported.')
        raw_hop = header_map.get(b'x-sheetbend-hop', b'0')
        try:
            hop = int(raw_hop)
            if len(raw_hop) > 2 or not 0 <= hop < 4:
                raise ValueError
        except ValueError:
            raise _HTTPError(508, 'Gateway hop limit exceeded or invalid hop header.', 'routing_loop') from None
        data = bytearray()
        async with asyncio.timeout(min(15, self.settings['request_timeout_seconds'])):
            while True:
                event = await receive()
                if event['type'] == 'http.disconnect':
                    raise _Disconnected
                if event['type'] != 'http.request':
                    continue
                data.extend(event.get('body', b''))
                if len(data) > self.settings['max_body_bytes']:
                    raise _HTTPError(413, 'Request body exceeds the configured limit.')
                if not event.get('more_body', False):
                    break
        try:
            body = json_object(bytes(data))
        except ProbeError:
            raise _HTTPError(400, 'Request must be a finite, unambiguous UTF-8 JSON object.') from None
        alias = body.get('model')
        if not isinstance(alias, str) or alias not in grant.routes:
            raise _HTTPError(403, 'The requested model route is not granted to this client.', 'route_denied')
        target = self._targets[alias]
        if path == '/v1/systemone':
            if not target.definition['capabilities']['system_one']:
                raise SelectionError('system_one is not declared.')
            payload = prepare_system_one(body)
        else:
            payload = prepare_chat(body, target.definition, target.reasoning)
        payload['model'] = target.model
        await self._while_connected(
            self._forward(target, alias, payload, path, hop, grant.application, send), receive,
        )

    async def _while_connected(self, operation: Any, receive: Any) -> None:
        async def disconnected() -> None:
            while True:
                event = await receive()
                if event['type'] == 'http.disconnect':
                    return
        work = asyncio.create_task(operation)
        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
            else:
                raise _Disconnected
        finally:
            watcher.cancel()
            work.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            # Always wait for cleanup, including cancelling a pending admission thread.
            with suppress(asyncio.CancelledError, Exception):
                await work

    async def _admit(self, target: _Target, operation: str, application: str) -> Any:
        cancelled = Event()
        task = asyncio.create_task(asyncio.to_thread(
            target.source._admission.acquire, model=target.model, operation=operation,
            application=application, tool=f'gateway:{operation}', cancelled=cancelled,
            wait_timeout=self.settings['queue_timeout_seconds'],
        ))
        try:
            return await asyncio.shield(task)
        except BaseException:
            cancelled.set()
            try:
                request = await asyncio.shield(task)
            except Exception:
                pass
            else:
                await asyncio.to_thread(request.finish, 'cancelled')
            raise

    async def _forward(self, target: _Target, alias: str, payload: dict[str, Any],
                       path: str, hop: int, application: str, send: Any) -> None:
        operation = 'system_one' if path.endswith('/systemone') else 'generate'
        request = await self._admit(target, operation, application)
        state, counts = 'error', (None, None)
        try:
            client = self._clients[target.source.name]
            endpoint = target.source.base_url.rstrip('/') + path.removeprefix('/v1')
            async with client.stream('POST', endpoint, json=payload,
                                     headers={'X-Sheetbend-Hop': str(hop + 1)}) as upstream:
                if not upstream.is_success:
                    raise _HTTPError(upstream.status_code if upstream.status_code >= 400 else 502,
                                     f'Upstream returned HTTP {upstream.status_code}; details withheld.',
                                     'upstream_error')
                if payload.get('stream', False):
                    if not upstream.headers.get('content-type', '').startswith('text/event-stream'):
                        raise ProbeError('Expected an event stream.')
                    await send({'type': 'http.response.start', 'status': 200, 'headers': [
                        (b'content-type', b'text/event-stream'), (b'cache-control', b'no-store'),
                        (b'x-accel-buffering', b'no'),
                    ]})
                    await asyncio.to_thread(request.streaming)
                    finished = False
                    async for event in self._events(upstream):
                        if event == b'[DONE]':
                            finished = True
                            await send({'type': 'http.response.body', 'body': b'data: [DONE]\n\n',
                                        'more_body': False})
                            break
                        body = json_object(event)
                        if 'error' in body:
                            raise ProbeError('Upstream stream error.')
                        if 'model' in body:
                            body['model'] = alias
                        new_counts = token_usage(body)
                        counts = tuple(old if new is None else new for old, new in zip(counts, new_counts))
                        encoded = json.dumps(body, allow_nan=False).encode('utf-8')
                        await send({'type': 'http.response.body', 'body': b'data: ' + encoded + b'\n\n',
                                    'more_body': True})
                    if not finished:
                        raise ProbeError('Upstream stream ended without a completion marker.')
                else:
                    body_bytes = bytearray()
                    async for chunk in upstream.aiter_bytes():
                        body_bytes.extend(chunk)
                        if len(body_bytes) > self.settings['max_response_bytes']:
                            raise ProbeError('Provider response exceeds the size limit.')
                    body = json_object(bytes(body_bytes))
                    if 'error' in body:
                        raise ProbeError('Upstream returned an error object.')
                    if operation == 'system_one':
                        validate_answers(payload, body)
                    elif not isinstance(body.get('choices'), list):
                        raise ProbeError('Invalid Chat Completions response.')
                    if 'model' in body:
                        body['model'] = alias
                    counts = token_usage(body)
                    await self._json(send, 200, body)
                state = 'done'
        except (asyncio.CancelledError, _Disconnected, OSError):
            state = 'cancelled'
            raise
        finally:
            await asyncio.shield(asyncio.to_thread(request.finish, state,
                                                   input_tokens=counts[0], output_tokens=counts[1]))

    async def _events(self, response: httpx.Response) -> Any:
        """Read bounded SSE data events without buffering an unbounded upstream line."""
        buffer = bytearray()
        data: list[bytes] = []
        event_size = total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self.settings['max_response_bytes']:
                raise ProbeError('Stream exceeds the configured response limit.')
            buffer.extend(chunk)
            while b'\n' in buffer:
                line, _, remaining = buffer.partition(b'\n')
                buffer = bytearray(remaining)
                line = line.rstrip(b'\r')
                event_size += len(line)
                if event_size > 1048576:
                    raise ProbeError('SSE event exceeds the size limit.')
                if not line:
                    if data:
                        yield b'\n'.join(data)
                    data, event_size = [], 0
                elif line.startswith(b'data:'):
                    data.append(bytes(line[5:]).lstrip(b' '))
            if len(buffer) + event_size > 1048576:
                raise ProbeError('SSE event exceeds the size limit.')
        if buffer or data:
            raise ProbeError('Truncated event stream.')

    @staticmethod
    async def _json(send: Any, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, allow_nan=False, ensure_ascii=False).encode('utf-8')
        await send({'type': 'http.response.start', 'status': status, 'headers': [
            (b'content-type', b'application/json'), (b'cache-control', b'no-store'),
            (b'content-length', str(len(data)).encode('ascii')),
        ]})
        await send({'type': 'http.response.body', 'body': data, 'more_body': False})


class GatewayServer:
    """A loopback-only Uvicorn lifecycle, shared by serve, run, and socket tests."""

    def __init__(self, app: Gateway, port: int | None = None) -> None:
        self.app = app
        self.port = app.settings['port'] if port is None else port
        self._thread: Thread | None = None
        self._socket: socket.socket | None = None
        self._server: Any = None
        self._failed = Event()

    @property
    def base_url(self) -> str:
        if self._socket is None:
            raise RuntimeError('Gateway is not started.')
        return f'http://127.0.0.1:{self._socket.getsockname()[1]}/v1'

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._failed.is_set()

    def start(self) -> 'GatewayServer':
        try:
            import uvicorn
        except ImportError:
            raise DependencyError("Gateway serving requires: pip install 'sheetbend[gateway]'") from None
        if self._thread is not None:
            raise RuntimeError('Gateway is already started.')
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(('127.0.0.1', self.port))
            sock.listen(128)
        except OSError:
            sock.close()
            raise ConfigError('Cannot bind the loopback gateway port.') from None
        self._socket = sock
        config = uvicorn.Config(
            self.app, loop='asyncio', http='h11', ws='none', lifespan='on', workers=1,
            log_config=None, log_level='critical', access_log=False, proxy_headers=False,
            server_header=False, limit_concurrency=self.app.settings['max_connections'],
            timeout_keep_alive=5, timeout_graceful_shutdown=2,
        )
        self._server = uvicorn.Server(config)

        def run() -> None:
            try:
                asyncio.run(self._server.serve(sockets=[sock]))
            except BaseException:
                self._failed.set()  # Never print a traceback with request/credential locals.

        self._thread = Thread(target=run, name='sheetbend-gateway', daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if not self._thread.is_alive() or self._failed.is_set() or time.monotonic() >= deadline:
                self.close()
                raise ConfigError('Gateway failed to start.')
            time.sleep(0.01)
        return self

    def close(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=12)
        if self._socket is not None:
            self._socket.close()
        self._socket = None
        if self._thread is not None and self._thread.is_alive():
            raise CoordinationError('Gateway shutdown did not complete within the shutdown deadline.')

    def __enter__(self) -> 'GatewayServer':
        return self.start()

    def __exit__(self, *args: Any) -> None:
        self.close()
