"""A diagnostic record whose external representation is defined by check.schema.json."""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One explicitly requested observation, not a lasting capability certification."""

    source: str
    model: str
    test: str
    ok: bool
    elapsed_seconds: float
    message: str
    model_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schema_version"] = 1
        data["model_ids"] = list(self.model_ids)
        return data

    def __str__(self) -> str:
        state = "PASS" if self.ok else "FAIL"
        return (
            f"{state} {self.source} / {self.test} ({self.elapsed_seconds:.3f}s): "
            f"{self.message}"
        )
