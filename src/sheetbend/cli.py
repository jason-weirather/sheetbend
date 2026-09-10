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
    """Discover, inspect, and verify intelligence sources. No inference CLI."""
    # Do not read configuration here: version/schema work even with broken config.
    ctx.obj = config_path


@main.command("version")
def version_command() -> None:
    """Print the package version."""
    click.echo(__version__)


@main.command("schema")
@click.option("--name", type=click.Choice(["config", "check", "check-report", "activity", "reasoning-plan"]), default="config")
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
    """Inspect one source (or the explicit configured default), offline."""
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
@click.option("--all", "all_tests", is_flag=True, help="Run all six probes, even undeclared capabilities.")
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
    if not all_tests and test == "models" and reasoning is not None:
        raise click.UsageError("--reasoning needs a generation --test or --all; the catalog does not generate.")
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
