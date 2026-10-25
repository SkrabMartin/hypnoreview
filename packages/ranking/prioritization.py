"""Deterministic global queue ordering and per-recording neighbor suppression."""

import hashlib

import numpy as np


def order_queue(
    rows: list[dict], strategy: str = "strict_priority", radius: int = 2, seed: int = 42
) -> list[int]:
    if isinstance(radius, bool) or not isinstance(radius, int) or radius < 0:
        raise ValueError("Coverage radius must be a nonnegative integer")
    keys = [(r["recording_id"], r["epoch_index"]) for r in rows]
    if len(keys) != len(set(keys)) or any(not isinstance(i, int) or i < 0 for _, i in keys):
        raise ValueError("Queue requires unique recording/nonnegative epoch identities")
    if any(not np.isfinite(r["review_score"]) or not 0 <= r["review_score"] <= 1 for r in rows):
        raise ValueError("Queue scores must be finite and in [0,1]")
    if strategy == "random":
        return sorted(
            range(len(rows)),
            key=lambda i: hashlib.sha256(f"{seed}:{keys[i][0]}:{keys[i][1]}".encode()).digest(),
        )
    if strategy not in ("strict_priority", "coverage_aware"):
        raise ValueError("Unknown review queue strategy")
    ordered = sorted(range(len(rows)), key=lambda i: (-rows[i]["review_score"], *keys[i]))
    if strategy == "strict_priority":
        return ordered
    chosen: dict[str, set[int]] = {}
    first, deferred = [], []
    for i in ordered:
        recording, epoch = keys[i]
        covered = chosen.setdefault(recording, set())
        if any(
            neighbor in covered for neighbor in range(max(0, epoch - radius), epoch + radius + 1)
        ):
            deferred.append(i)
        else:
            first.append(i)
            covered.add(epoch)
    return first + deferred
