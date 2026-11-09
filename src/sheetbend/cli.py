"""Click commands are a presentation layer over the public library."""

from functools import wraps
import json
import math
import time
from pathlib import Path
from typing import Any, Callable

import click

from ._version import __version__
from .config import load_schema
from .errors import SheetbendError
from .registry import Registry
from .runtime.runtime import Runtime
from .checks import TESTS
from .reasoning import reasoning_choices


def _errors(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def invoke(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except SheetbendError as exc:
            raise click.ClickException(str(exc)) from exc
    return invoke


def _json(value: Any) -> None:
    click.echo(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


@click.group(context_settings={"show_default": True})
@click.option("--config-path", type=click.Path(path_type=Path), help="Select one TOML file.")
@click.pass_context
def main(ctx: click.Context, config_path: Path | None) -> None:
    """Discover, verify, and expose approved intelligence sources."""
    # Do not read configuration here: version/schema work even with broken config.
    ctx.obj = config_path


@main.command("version")
def version_command() -> None:
    """Print the package version."""
    click.echo(__version__)


@main.command("schema")
@click.option("--name", type=click.Choice(["config", "check", "check-report", "activity", "reasoning-plan", "dashboard"]), default="config")
def schema_command(name: str) -> None:
    """Print a packaged JSON Schema."""
    _json(load_schema(name))


@main.command("sources")
@click.option("--json", "as_json", is_flag=True, help="Print normalized config as JSON.")
@click.pass_obj
@_errors
def sources_command(config_path: Path | None, as_json: bool) -> None:
    """List configured sources offline, without resolving credentials."""
    registry = Registry.from_file(config_path)
    if as_json:
        _json(registry.to_dict())
        return
    click.echo(f"Configuration: {registry.config_path}")
    if not registry.source_names:
        click.echo("No sources configured.")
        return
    for name in registry.source_names:
        source = registry.source(name, allowed_scopes=("local", "institutional", "external"))
        default = " *" if name == registry.default_source else ""
        click.echo(f"{name}{default}  [{source.scope}]  {source.default_model}")


@main.command("inspect")
@click.argument("source_name", required=False)
@click.option("--json", "as_json", is_flag=True, help="Print the normalized source definition.")
@click.pass_obj
@_errors
def inspect_command(config_path: Path | None, source_name: str | None, as_json: bool) -> None:
    """Inspect one explicit or deterministically resolved source, offline."""
    source = Registry.from_file(config_path).source(
        source_name, allowed_scopes=("local", "institutional", "external")
    )
    if as_json:
        _json(source.to_dict())
    else:
        click.echo(repr(source))
        _json(source.to_dict())


@main.command("check")
@click.argument("source_name", required=False)
@click.option("--test", type=click.Choice(TESTS), default="models")
@click.option("--all", "all_tests", is_flag=True, help="Run chat probes and/or declared System One probes.")
@click.option("--model", help="Explicit diagnostic model ID; may be unconfigured.")
@click.option(
    "--reasoning", type=click.Choice(reasoning_choices()), default=None,
    show_default="model's configured default",
    help="Reasoning choice for generation probes; unsupported choices fail without fallback.",
)
@click.option("--allow-external", is_flag=True, help="Explicitly allow a synthetic probe to an external source.")
@click.option("--json", "as_json", is_flag=True, help="Print a versioned diagnostic record/report.")
@click.pass_context
@_errors
def check_command(
    ctx: click.Context, source_name: str | None, test: str, all_tests: bool,
    model: str | None, reasoning: str | None, allow_external: bool, as_json: bool,
) -> None:
    """Verify one source. Generation probes send synthetic inputs and may cost money."""
    if all_tests and ctx.get_parameter_source("test") != click.core.ParameterSource.DEFAULT:
        raise click.UsageError("Use --all or --test, not both.")
    if not all_tests and test in {"models", "credentials", "system_one"} and reasoning is not None:
        raise click.UsageError("--reasoning needs a chat generation --test or --all; the catalog does not generate.")
    scopes = ("local", "institutional", "external") if allow_external else ("local", "institutional")
    source = Registry.from_file(ctx.obj).source(source_name, allowed_scopes=scopes)
    result = (source.check_all(model=model, reasoning=reasoning) if all_tests
              else source.check(test=test, model=model, reasoning=reasoning))
    if as_json:
        _json(result.to_dict())
    else:
        click.echo(str(result))
    if not result.ok:
        raise click.exceptions.Exit(1)


@main.command("top")
@click.option("--once", is_flag=True, help="Print one snapshot and exit; suitable for redirected output.")
@click.option("--json", "as_json", is_flag=True, help="Print one activity-schema JSON snapshot and exit.")
@click.option("--refresh-seconds", type=click.FloatRange(min=0.1), default=1.0)
@_errors
def top_command(once: bool, as_json: bool, refresh_seconds: float) -> None:
    """See this user's Sheetbend requests across applications on this host."""
    from rich.console import Console
    from rich.live import Live
    from .runtime.display import activity_table

    if not math.isfinite(refresh_seconds):
        raise click.BadParameter("must be finite", param_hint="--refresh-seconds")
    runtime = Runtime()
    snapshot = runtime.snapshot()
    if as_json:
        _json(snapshot)
        return
    console = Console()
    if once:
        console.print(activity_table(snapshot))
        return
    if not console.is_terminal:
        raise click.UsageError("Interactive top needs a terminal; use --once or --json.")
    try:
        with Live(activity_table(snapshot), console=console, screen=True, auto_refresh=False) as live:
            while True:
                time.sleep(refresh_seconds)
                live.update(activity_table(runtime.snapshot()), refresh=True)
    except KeyboardInterrupt:
        return


@main.command('serve')
@click.option('--port', type=click.IntRange(0, 65535), default=None,
              help='Override the configured loopback port; 0 chooses an available port.')
@click.pass_obj
@_errors
def serve_command(config_path: Path | None, port: int | None) -> None:
    """Serve authenticated, explicitly granted gateway routes on 127.0.0.1."""
    import signal
    from threading import Event
    from .errors import CoordinationError
    from .gateway import Gateway, GatewayServer
    registry = Registry.from_file(config_path)
    app = Gateway.configured(registry)
    stopped = Event()
    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda *_: stopped.set())
        with GatewayServer(app, port=port) as server:
            click.echo(f'Gateway: {server.base_url} (authenticated; loopback only)', err=True)
            while not stopped.wait(0.25):
                if not server.is_alive:
                    raise CoordinationError('Gateway stopped unexpectedly.')
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@main.command('run', context_settings={'ignore_unknown_options': True})
@click.option('--source', 'source_name', default=None, help='Select an explicit source.')
@click.option('--model', default=None, help='A configured model; defaults to the source default.')
@click.option('--reasoning', type=click.Choice(reasoning_choices()), default=None)
@click.option('--adapter', type=click.Choice(['openai', 'sheetbend']), default='openai')
@click.option('--application', default='sheetbend-run', help='Stable caller label, not argv.')
@click.option('--allow-external', is_flag=True, help='Permit explicitly selected external sources.')
@click.option('--inherit-env', multiple=True, help='Additionally inherit this named environment variable.')
@click.argument('command', nargs=-1, type=click.UNPROCESSED, required=True)
@click.pass_obj
@_errors
def run_command(config_path: Path | None, source_name: str | None, model: str | None,
                reasoning: str | None, adapter: str, application: str, allow_external: bool,
                inherit_env: tuple[str, ...], command: tuple[str, ...]) -> None:
    """Run COMMAND behind a temporary gateway. Use -- before the child command.

    The child receives a gateway session token, NEVER the resolved upstream key.
    Ordinary exit codes are preserved; signal termination returns 128 + signal.
    """
    import re
    from .runtime.process import caller_labels
    from .launch import run_with_gateway
    try:
        caller_labels(application, None)
    except ValueError:
        raise click.BadParameter('must be a printable label of at most 80 characters.',
                                 param_hint='--application') from None
    if any(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) is None for name in inherit_env):
        raise click.BadParameter('must be an environment variable name.', param_hint='--inherit-env')
    scopes = ('local', 'institutional', 'external') if allow_external else ('local', 'institutional')
    source = Registry.from_file(config_path).source(source_name, allowed_scopes=scopes)
    code = run_with_gateway(source, command, model=model, reasoning=reasoning, adapter=adapter,
                            application=application, inherit_env=inherit_env)
    raise click.exceptions.Exit(code)


@main.command('dashboard')
@click.option('--probe', is_flag=True, help='Read referenced credentials and GET catalogs once; no inference.')
@click.option('--watch', is_flag=True, help='Keep the inventory on screen; checks are cached, not per-frame.')
@click.option('--test', 'inference_test', type=click.Choice(['text', 'system_one']), default=None,
              help='Explicitly test one configured model ONCE; may incur charges.')
@click.option('--source', 'source_name', default=None, help='Source for the one-shot inference test.')
@click.option('--model', default=None, help='Model for the one-shot test; default is the source default.')
@click.option('--interval-seconds', type=click.FloatRange(min=60), default=60,
              help='Minimum time between cheap probes in watch mode.')
@click.option('--plain', is_flag=True, help='ASCII only, no ANSI color or full-screen controls.')
@click.option('--json', 'as_json', is_flag=True, help='Print one versioned inventory/status snapshot.')
@click.option('--allow-external', is_flag=True, help='Also probe external sources; otherwise only list them.')
@click.pass_obj
@_errors
def dashboard_command(config_path: Path | None, probe: bool, watch: bool, interval_seconds: float,
                      plain: bool, as_json: bool, allow_external: bool,
                      inference_test: str | None, source_name: str | None, model: str | None) -> None:
    """View configured models, credential access, and gateway routes. Offline by default."""
    from rich.console import Console
    from rich.live import Live
    from .dashboard import Dashboard, plain_console_requested, render_dashboard
    if not math.isfinite(interval_seconds):
        raise click.BadParameter('must be finite', param_hint='--interval-seconds')
    if watch and as_json:
        raise click.UsageError('--watch and --json are exclusive.')
    if inference_test is not None and source_name is None:
        raise click.UsageError('--test requires an explicit --source.')
    if inference_test is None and (source_name is not None or model is not None):
        raise click.UsageError('--source/--model require a one-shot --test.')
    console = Console()
    plain = plain_console_requested(console, plain)
    if plain:
        console = Console(force_terminal=False, no_color=True)
    dashboard = Dashboard(Registry.from_file(config_path), allow_external=allow_external)
    if inference_test is not None:
        # Validate the caller boundary and model before any optional catalog work.
        source = dashboard.registry.source(source_name, allowed_scopes=(
            ('local', 'institutional', 'external') if allow_external else ('local', 'institutional')))
        source._selected_model(model)
    if probe:
        dashboard.probe()
    if inference_test is not None:
        dashboard.test(source_name, model=model, test=inference_test)
    last_probe = time.monotonic()
    snapshot = dashboard.snapshot()
    if as_json:
        _json(snapshot)
        return
    if not watch:
        console.print(render_dashboard(snapshot, plain=plain))
        return
    try:
        if plain:
            while True:
                console.print(render_dashboard(dashboard.snapshot(), plain=True))
                time.sleep(interval_seconds)
                if probe:
                    dashboard.probe()
        else:
            with Live(render_dashboard(snapshot), console=console, screen=True, auto_refresh=False) as live:
                while True:
                    time.sleep(1)
                    if probe and time.monotonic() - last_probe >= interval_seconds:
                        dashboard.probe()
                        last_probe = time.monotonic()
                    live.update(render_dashboard(dashboard.snapshot()), refresh=True)
    except KeyboardInterrupt:
        return
