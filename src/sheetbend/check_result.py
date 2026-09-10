"""Schema-defined diagnostic observations, separate from capability declarations."""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One timestamped observation, not a capability certification."""

    source: str
    model: str
    test: str
    ok: bool
    elapsed_seconds: float
    message: str
    observed_at: float
    declared: bool | None
    model_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schema_version"] = 2
        data["model_ids"] = list(self.model_ids)
        return data

    def __str__(self) -> str:
        state = "PASS" if self.ok else "FAIL"
        return f"{state} {self.source} / {self.model} / {self.test} ({self.elapsed_seconds:.3f}s): {self.message}"


@dataclass(frozen=True, slots=True)
class CheckReport:
    """An explicit multi-probe diagnostic, retaining every individual observation."""

    results: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(result.ok for result in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, "ok": self.ok,
                "results": [result.to_dict() for result in self.results]}

    def __str__(self) -> str:
        return "\n".join(str(result) for result in self.results)
