"""Half-open recording-relative epochs, with conservative annotation coverage."""

from dataclasses import dataclass
from math import floor, isfinite

from packages.core.annotations import STAGES, Annotation


@dataclass(frozen=True)
class Epoch:
    index: int
    start_s: float
    duration_s: float
    complete: bool
    reference_stage: str | None
    valid_reference: bool
    covered_s: float
    conflict_s: float
    unknown_s: float
    original_labels: tuple[str, ...]
    reasons: tuple[str, ...]


def align_epochs(
    duration_s: float,
    annotations: list[Annotation],
    epoch_s: float = 30.0,
    minimum_coverage_s: float = 29.9,
) -> list[Epoch]:
    """Integrate union durations, never sum overlapping annotations twice.

    Different original labels within an epoch are ambiguous even if their
    normalized class agrees (historical 3 vs 4 is preserved). Unknown/conflicting
    spans invalidate references regardless of the coverage threshold. A small
    uncovered rounding gap alone is permitted by the documented 29.9s rule.
    """
    if not isfinite(duration_s) or duration_s <= 0:
        raise ValueError("Recording duration must be finite and positive")
    if epoch_s != 30 or not 0 < minimum_coverage_s <= epoch_s:
        raise ValueError("Only 30-second epochs with coverage in (0, 30] are supported")
    ordered = sorted(annotations, key=lambda a: a.start_s)
    count = floor(duration_s / epoch_s)
    tail = duration_s - count * epoch_s
    result = []
    for index in range(count + (tail > 1e-8)):
        start = index * epoch_s
        length = min(epoch_s, duration_s - start)
        stop = start + length
        intersecting = [a for a in ordered if a.start_s < stop and a.end_s > start]
        boundaries = sorted(
            {
                start,
                stop,
                *(max(start, a.start_s) for a in intersecting),
                *(min(stop, a.end_s) for a in intersecting),
            }
        )
        covered = conflict = unknown = 0.0
        labels: set[str] = set()
        for left, right in zip(boundaries, boundaries[1:]):
            active = {
                a.original_label for a in intersecting if a.start_s < right and a.end_s > left
            }
            if not active:
                continue
            labels.update(active)
            covered += right - left
            if len(active) > 1:
                conflict += right - left
            if any(a.stage is None for a in intersecting if a.original_label in active):
                unknown += right - left
        complete = length >= epoch_s - 1e-8
        label = next(iter(labels)) if len(labels) == 1 else None
        stage = next((a.stage for a in intersecting if a.original_label == label), None)
        reasons = []
        if not complete:
            reasons.append("incomplete_tail")
        if covered < minimum_coverage_s - 1e-8:
            reasons.append("insufficient_coverage")
        if conflict > 0:
            reasons.append("conflicting_annotations")
        if len(labels) > 1:
            reasons.append("mixed_labels")
        if unknown > 0:
            reasons.append("unknown_annotation")
        if stage is not None and stage not in STAGES:
            reasons.append("excluded_reference_category")
        valid = complete and not reasons and stage in STAGES
        result.append(
            Epoch(
                index,
                start,
                length,
                complete,
                stage,
                valid,
                covered,
                conflict,
                unknown,
                tuple(sorted(labels)),
                tuple(reasons),
            )
        )
    return result
