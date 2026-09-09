"""Click commands are a presentation layer over the public library."""

from functools import wraps
import json
from pathlib import Path
from typing import Any, Callable

import click

from ._version import __version__
from .config import load_schema
from .errors import SheetbendError
from .registry import Registry


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
@click.option("--name", type=click.Choice(["config", "check"]), default="config")
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
        source = registry.source(name)
        default = " *" if name == registry.default_source else ""
        click.echo(f"{name}{default}  [{source.scope}]  {source.default_model}")


@main.command("inspect")
@click.argument("source_name", required=False)
@click.option("--json", "as_json", is_flag=True, help="Print the normalized source definition.")
@click.pass_obj
@_errors
def inspect_command(config_path: Path | None, source_name: str | None, as_json: bool) -> None:
    """Inspect one source (or the explicit configured default), offline."""
    source = Registry.from_file(config_path).source(source_name)
    if as_json:
        _json(source.to_dict())
    else:
        click.echo(repr(source))
        _json(source.to_dict())


@main.command("check")
@click.argument("source_name", required=False)
@click.option("--test", type=click.Choice(["models", "text", "schema", "stream"]), default="models")
@click.option("--model", help="Explicit model override; does not change the configuration.")
@click.option("--json", "as_json", is_flag=True, help="Print a versioned diagnostic record.")
@click.pass_obj
@_errors
def check_command(
    config_path: Path | None, source_name: str | None, test: str, model: str | None, as_json: bool
) -> None:
    """Verify one source. Text/schema/stream send a synthetic prompt and may cost money."""
    source = Registry.from_file(config_path).source(source_name)
    result = source.check(test=test, model=model)
    if as_json:
        _json(result.to_dict())
    else:
        click.echo(str(result))
    if not result.ok:
        raise click.exceptions.Exit(1)
