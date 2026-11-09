"""Inventory first, explicit cheap probes second. Never schedule generation automatically."""

from copy import deepcopy
import os
import time
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from .errors import CredentialError
from .registry import Registry

_SCOPES = ('local', 'institutional', 'external')


class Dashboard:
    """A process-local cache of observations, not secret values or a persistent health daemon."""

    def __init__(self, registry: Registry, *, allow_external: bool = False) -> None:
        self.registry = registry
        self.allow_external = allow_external
        self._last_probe: float | None = None
        self._credentials: dict[str, dict[str, Any]] = {
            name: {'name': name, 'provider': registry.credentials.definition(name)['provider'],
                   'status': 'unchecked', 'observed_at': None}
            for name in registry.credentials.names
        }
        self._tests: dict[tuple[str, str], dict[str, Any]] = {}
        self._sources: dict[str, dict[str, Any]] = {}
        for name in registry.source_names:
            source = registry.source(name, allowed_scopes=_SCOPES)
            auth = source.to_dict()['auth']
            self._sources[name] = {
                'source': name, 'scope': source.scope,
                'credential': auth.get('credential', f"env:{auth['env']}" if 'env' in auth else auth['type']),
                'credential_status': 'unchecked', 'catalog_status': 'unchecked',
                'model_ids': [], 'observed_at': None,
            }

    def probe(self) -> bool:
        """At most once per 60 seconds per dashboard instance, including failures.

        One catalog GET per permitted source. Five-second HTTP inactivity timeout,
        one-second admission timeout. Keychain may prompt; no inference is sent.
        """
        now = time.monotonic()
        if self._last_probe is not None and now - self._last_probe < 60:
            return False
        self._last_probe = now
        checked_credentials = set()
        for name, observation in self._sources.items():
            source = self.registry.source(name, allowed_scopes=_SCOPES)
            observation.update(model_ids=[], observed_at=time.time(), catalog_status='unchecked',
                               credential_status='unchecked')
            if source.scope == 'external' and not self.allow_external:
                observation['catalog_status'] = 'blocked'
                continue
            reference = source.to_dict()['auth'].get('credential')
            try:
                headers = source.resolve_auth()
                observation['credential_status'] = 'available'
                if reference is not None:
                    self._credentials[reference].update(status='available', observed_at=time.time())
                    checked_credentials.add(reference)
            except CredentialError:
                observation['credential_status'] = 'unavailable'
                if reference is not None:
                    self._credentials[reference].update(status='unavailable', observed_at=time.time())
                    checked_credentials.add(reference)
                continue
            try:
                ids = source.list_models(application='sheetbend', tool='dashboard:catalog',
                                         timeout_seconds=5, queue_timeout_seconds=1, _headers=headers)
                observation.update(model_ids=ids, catalog_status='reachable')
            except Exception:
                # Deliberately do not retain provider exceptions or raw response contents.
                observation['catalog_status'] = 'unavailable'
        # Incoming tokens matter too, but unused credential references are never fetched.
        clients = self.registry.to_dict().get('gateway', {}).get('clients', {})
        for client in clients.values():
            reference = client['credential']
            if reference in checked_credentials:
                continue
            try:
                self.registry.credentials.resolve(reference)
                status = 'available'
            except CredentialError:
                status = 'unavailable'
            self._credentials[reference].update(status=status, observed_at=time.time())
            checked_credentials.add(reference)
        return True

    def test(self, source_name: str, *, model: str | None = None, test: str = 'text') -> Any:
        """Explicitly test one configured model once. Never called by watch refreshes."""
        if test not in {'text', 'system_one'}:
            raise ValueError('Dashboard inference tests are text or system_one.')
        scopes = _SCOPES if self.allow_external else ('local', 'institutional')
        source = self.registry.source(source_name, allowed_scopes=scopes)
        selected = source._selected_model(model)
        result = source.check(test=test, model=selected)
        self._tests[(source_name, selected)] = {
            'inference_status': 'passed' if result.ok else 'failed',
            'inference_test': test, 'inference_observed_at': result.observed_at,
        }
        return result

    def snapshot(self) -> dict[str, Any]:
        rows = []
        for name, observation in self._sources.items():
            source = self.registry.source(name, allowed_scopes=_SCOPES)
            for model, definition in source.to_dict()['models'].items():
                status = ('listed' if model in observation['model_ids'] else 'absent') \
                    if observation['catalog_status'] == 'reachable' else 'unknown'
                rows.append({**{k: v for k, v in observation.items() if k != 'model_ids'},
                             'model': model, 'model_status': status,
                             'capabilities': [k for k, v in definition['capabilities'].items() if v],
                             **self._tests.get((name, model), {'inference_status': 'not_tested',
                                 'inference_test': None, 'inference_observed_at': None})})
        settings = self.registry.to_dict().get('gateway', {})
        routes = [
            {'route': name, 'source': definition['source'], 'model': definition['model'],
             'clients': [client for client, grant in settings.get('clients', {}).items()
                         if name in grant['routes']]}
            for name, definition in settings.get('routes', {}).items()
        ]
        return deepcopy({'schema_version': 1, 'observed_at': time.time(),
                         'models': rows, 'credentials': list(self._credentials.values()),
                         'routes': routes})


def _safe(value: object, plain: bool) -> str:
    text = str(value)
    text = ''.join(c if c.isprintable() else '?' for c in text)
    return text.encode('ascii', 'backslashreplace').decode('ascii') if plain else text


def render_dashboard(snapshot: dict[str, Any], *, plain: bool = False) -> Group:
    table = Table(title='Sheetbend | configured model inventory', box=box.ASCII if plain else box.ROUNDED)
    for name in ('SOURCE / SCOPE', 'MODEL', 'CAPABILITIES', 'CREDENTIAL', 'CATALOG', 'MODEL STATUS', 'INFERENCE', 'AGE'):
        table.add_column(name, overflow='fold')
    colors = {'available': 'green', 'reachable': 'green', 'listed': 'green', 'absent': 'yellow',
              'unavailable': 'red', 'blocked': 'yellow', 'passed': 'green', 'failed': 'red', 'unchecked': 'dim', 'unknown': 'dim'}
    for row in snapshot['models']:
        age = '?' if row['observed_at'] is None else f"{max(0, snapshot['observed_at'] - row['observed_at']):.0f}s"
        values = [f"{row['source']} / {row['scope']}", row['model'], ', '.join(row['capabilities']),
                  f"{row['credential']}: {row['credential_status']}", row['catalog_status'],
                  row['model_status'], row['inference_status'], age]
        cells = [Text(_safe(v, plain)) for v in values]
        for index, status in ((3, row['credential_status']), (4, row['catalog_status']), (5, row['model_status']), (6, row['inference_status'])):
            if not plain:
                cells[index].stylize(colors.get(status, ''))
        table.add_row(*cells)
    credentials = Table(title='Credential references (values never shown)', box=box.ASCII if plain else box.SIMPLE)
    for name in ('REFERENCE', 'PROVIDER', 'ACCESS'):
        credentials.add_column(name)
    for row in snapshot['credentials']:
        credentials.add_row(Text(_safe(row['name'], plain)), Text(row['provider']),
                            Text(row['status'], style='' if plain else colors.get(row['status'], '')))
    routes = Table(title='Configured gateway routes (not a live listener check)', box=box.ASCII if plain else box.SIMPLE)
    for name in ('ROUTE', 'TARGET', 'GRANTED CLIENTS'):
        routes.add_column(name)
    for row in snapshot['routes']:
        routes.add_row(*(Text(_safe(v, plain)) for v in
                         [row['route'], f"{row['source']} / {row['model']}", ', '.join(row['clients']) or '(none)']))
    note = Text('Listed != loaded or inference-tested. --probe reads credentials and catalogs only.\n'
                '--test text|system_one --source SOURCE --model ID tests one model once, never on refresh.')
    return Group(table, credentials, routes, note)


def plain_console_requested(console: Console, plain: bool) -> bool:
    return plain or not console.is_terminal or os.environ.get('TERM') == 'dumb' or 'NO_COLOR' in os.environ
