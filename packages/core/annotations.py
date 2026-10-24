"""Original R&K labels remain separate from normalized reference stages."""

from dataclasses import dataclass
from math import isfinite

LABEL_MAP = {
    "Sleep stage W": "WAKE",
    "Sleep stage 1": "N1",
    "Sleep stage 2": "N2",
    "Sleep stage 3": "N3",
    "Sleep stage 4": "N3",
    "Sleep stage R": "REM",
    "Sleep stage M": "ARTIFACT_OR_MOVEMENT",
    "Sleep stage ?": "UNSCORED",
}
STAGES = frozenset(("WAKE", "N1", "N2", "N3", "REM"))


@dataclass(frozen=True)
class Annotation:
    start_s: float
    duration_s: float
    original_label: str

    def __post_init__(self) -> None:
        if (
            not isfinite(self.start_s)
            or not isfinite(self.duration_s)
            or not isfinite(self.start_s + self.duration_s)
            or self.duration_s < 0
        ):
            raise ValueError(
                "Annotation onset/duration must be finite; duration cannot be negative"
            )

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s

    @property
    def stage(self) -> str | None:
        return LABEL_MAP.get(self.original_label)
