"""Resolve declared reasoning controls without owning prompting or provider discovery."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .config import load_schema
from .errors import SelectionError


def reasoning_choices() -> tuple[str, ...]:
    """Read the public vocabulary from the authoritative configuration schema."""
    return tuple(load_schema()["$defs"]["reasoning_choice"]["enum"])


def validate_reasoning_choice(reasoning: str | None) -> None:
    """Validate a caller choice; None means use the selected model's configured default."""
    if reasoning is not None and (
        not isinstance(reasoning, str) or reasoning not in reasoning_choices()
    ):
        raise ValueError(f"reasoning must be None or one of {reasoning_choices()}.")


@dataclass(frozen=True, slots=True)
class ReasoningPlan:
    """An immutable offline resolution defined by reasoning-plan.schema.json.

    parameter/value describe the request control, not observed provider behavior.
    None means no reasoning parameter is sent. This does not mean reasoning is off.
    """

    source: str
    model: str
    requested: str | None
    selected: str
    origin: str
    control: str
    parameter: str | None
    value: str | bool | None

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, **asdict(self)}

    def __str__(self) -> str:
        wire = "no override" if self.parameter is None else f"{self.parameter}={self.value!r}"
        return (
            f"{self.source} / {self.model}: reasoning={self.selected} "
            f"({self.origin}; {self.control}; {wire})"
        )


def resolve_reasoning(
    definition: Mapping[str, Any], *, source: str, model: str, requested: str | None = None,
) -> ReasoningPlan:
    """Resolve a schema-validated declaration. Never modify it or soften a requirement."""
    validate_reasoning_choice(requested)
    selected = definition["default"] if requested is None else requested
    control = definition["control"]
    parameter: str | None = None
    value: str | bool | None = None
    if selected == "provider":
        supported = True
    elif control == "fixed-off":
        supported = selected == "off"
    elif control == "fixed-on":
        supported = selected == "on"
    elif control == "reasoning_effort":
        supported = selected in definition["values"]
        if supported:
            parameter, value = "reasoning_effort", definition["values"][selected]
    elif control == "chat_template_kwargs":
        supported = selected in {"off", "on"}
        if supported:
            parameter, value = "chat_template_kwargs.enable_thinking", selected == "on"
    else:
        supported = False  # Unknown is not evidence of a fixed non-reasoning model.
    if not supported:
        raise SelectionError(
            f"Source {source!r}, model {model!r}: reasoning={selected!r} is not supported "
            f"by its declared reasoning control {control!r}. Configure the model's "
            "reasoning declaration or explicitly choose a supported setting; no fallback was made."
        )
    return ReasoningPlan(
        source=source, model=model, requested=requested, selected=selected,
        origin="model-default" if requested is None else "caller", control=control,
        parameter=parameter, value=value,
    )
