"""A terminal view over Runtime.snapshot(), not a second telemetry implementation."""

from typing import Any

from rich.table import Table
from rich.text import Text


def activity_table(snapshot: dict[str, Any]) -> Table:
    """Render content-free activity; Text cells cannot interpret user labels as markup."""
    table = Table(title=Text(f"Sheetbend | {snapshot['host']} | same user, same host"), expand=True)
    for name in ("PID", "APPLICATION", "TOOL", "SOURCE", "MODEL", "STATE", "AGE", "TOKENS"):
        table.add_column(name, overflow="ellipsis", no_wrap=True)
    for request in snapshot["requests"]:
        counts = ["?" if request[key] is None else str(request[key]) for key in ("input_tokens", "output_tokens")]
        values = [str(request["pid"]), request["application"], request["tool"] or "·",
                  request["source"], request["model"] or "(catalog)", request["state"],
                  f"{request['age_seconds']:.1f}s", " → ".join(counts)]
        table.add_row(*(Text(value) for value in values))
    table.caption = Text("One row per request. Tokens are provider-reported. Ctrl-C exits without affecting callers.")
    return table
