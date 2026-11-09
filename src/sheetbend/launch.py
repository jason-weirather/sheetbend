"""Launch one process behind a temporary gateway without mutating os.environ."""

from collections.abc import Sequence
import os
import re
import signal
import subprocess
from threading import current_thread, main_thread
import time

from .errors import SelectionError, SheetbendError
from .gateway import Gateway, GatewayServer
from .source import Source

_SAFE_ENV = {
    'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'TERM', 'COLORTERM', 'NO_COLOR',
    'TMPDIR', 'TMP', 'TEMP', 'VIRTUAL_ENV', 'CONDA_PREFIX', 'CONDA_DEFAULT_ENV',
    'CONDA_SHLVL', 'PYTHONPATH', 'PYTHONUTF8', 'PYTHONIOENCODING',
    'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME',
}


def child_environment(base_url: str, token: str, adapter: str,
                      inherit_env: Sequence[str] = ()) -> dict[str, str]:
    """The returned mapping contains a secret session token; do not log it."""
    if adapter not in {'openai', 'sheetbend'}:
        raise ValueError('Unsupported environment adapter.')
    for name in inherit_env:
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) is None:
            raise ValueError('Invalid environment variable name.')
    names = _SAFE_ENV | set(inherit_env)
    result = {key: value for key, value in os.environ.items()
              if key in names or key.startswith('LC_')}
    result.update(SHEETBEND_BASE_URL=base_url, SHEETBEND_API_KEY=token,
                  SHEETBEND_MODEL='selected', NO_PROXY='127.0.0.1,localhost,::1',
                  no_proxy='127.0.0.1,localhost,::1')
    if adapter == 'openai':
        result.update(OPENAI_BASE_URL=base_url, OPENAI_API_KEY=token)
    return result


def run_with_gateway(source: Source, command: Sequence[str], *, model: str | None = None,
                     reasoning: str | None = None, adapter: str = 'openai',
                     application: str = 'sheetbend-run', inherit_env: Sequence[str] = ()) -> int:
    """Return the child exit status (128+signal for signal termination).

    Normal environment inheritance is allow-listed. This is credential delivery,
    not process isolation or a defense against hostile code running as the same user.
    """
    if not command:
        raise ValueError('A child command is required.')
    selected = source._selected_model(model)
    if adapter == 'openai' and not source._model_definition(selected)['capabilities']['chat']:
        raise SelectionError('The openai adapter requires chat; use --adapter sheetbend for System One.')
    app, token = Gateway.temporary(source, model=selected, reasoning=reasoning, application=application)
    with GatewayServer(app, port=0) as gateway:
        environment = child_environment(gateway.base_url, token, adapter, inherit_env)
        try:
            child = subprocess.Popen(list(command), env=environment, start_new_session=True)
        except OSError:
            raise SheetbendError('Unable to start the child command; arguments withheld.') from None
        previous = {}
        deadline: float | None = None

        def forward(signum: int, _frame: object) -> None:
            nonlocal deadline
            deadline = time.monotonic() + 5 if deadline is None else time.monotonic()
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

        try:
            if current_thread() is main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    previous[signum] = signal.signal(signum, forward)
            while child.poll() is None:
                if not gateway.is_alive:
                    raise SheetbendError('Gateway stopped before the child command completed.')
                if deadline is not None and time.monotonic() >= deadline:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                time.sleep(0.02)
            code = child.wait()
            return code if code >= 0 else 128 - code
        finally:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
