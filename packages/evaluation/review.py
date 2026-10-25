"""Equal-budget simulated error discovery with participant-cluster intervals."""

from math import ceil

import numpy as np


def review_curve(errors: list[bool], order: list[int], budgets: list[float]) -> dict:
    n = len(errors)
    if (
        not n
        or len(order) != n
        or any(isinstance(i, bool) or not isinstance(i, int) for i in order)
        or set(order) != set(range(n))
    ):
        raise ValueError("Review order must be a full permutation of a nonempty evaluation pool")
    if (
        not budgets
        or any(not np.isfinite(b) or not 0 <= b <= 1 for b in budgets)
        or budgets != sorted(set(budgets))
    ):
        raise ValueError("Review budgets must be sorted, unique fractions in [0,1]")
    if any(not isinstance(e, (bool, np.bool_)) for e in errors):
        raise ValueError("Error indicators must be Boolean")
    cumulative = np.r_[0, np.cumsum(np.asarray(errors)[order])]
    checked = [min(n, ceil(n * b - 1e-10)) for b in budgets]
    return {
        "n_epochs": n,
        "n_errors": int(sum(errors)),
        "reviewed": checked,
        "found": [int(cumulative[k]) for k in checked],
    }


def aggregate_review(
    records: list[dict], budgets: list[float], bootstrap_samples: int = 500, seed: int = 42
) -> dict:
    if not records or not isinstance(bootstrap_samples, int) or bootstrap_samples < 1:
        raise ValueError("Nonempty evaluation records and positive bootstrap count required")
    subjects = sorted({r["subject_id"] for r in records})
    methods = sorted(records[0]["methods"])
    review_curve([False], [0], budgets)  # shared budget validation
    if not methods:
        raise ValueError("At least one review method required")
    if any(set(r["methods"]) != set(methods) for r in records):
        raise ValueError("Every recording must use the same comparison methods")
    for record in records:
        reference = record["methods"][methods[0]]
        for curve in record["methods"].values():
            expected = [ceil(curve["n_epochs"] * b - 1e-10) for b in budgets]
            if (
                curve["reviewed"] != expected
                or not 0 <= curve["n_errors"] <= curve["n_epochs"]
                or any(
                    found < 0 or found > min(checked, curve["n_errors"])
                    for found, checked in zip(curve["found"], curve["reviewed"], strict=True)
                )
            ):
                raise ValueError("Invalid reference pool or per-recording budgets")
        if any(
            (curve["n_epochs"], curve["n_errors"], curve["reviewed"])
            != (reference["n_epochs"], reference["n_errors"], reference["reviewed"])
            for curve in record["methods"].values()
        ):
            raise ValueError("Methods must share the same reference pool and per-recording budgets")
    draws = np.random.default_rng(seed).integers(
        0, len(subjects), size=(bootstrap_samples, len(subjects))
    )
    output: dict[str, dict] = {}
    for method in methods:
        groups = []
        for subject in subjects:
            curves = [r["methods"][method] for r in records if r["subject_id"] == subject]
            if any(
                len(c["reviewed"]) != len(budgets) or len(c["found"]) != len(budgets)
                for c in curves
            ):
                raise ValueError("Curve lengths must match budgets")
            groups.append(
                {
                    "n_epochs": sum(c["n_epochs"] for c in curves),
                    "n_errors": sum(c["n_errors"] for c in curves),
                    "reviewed": np.sum([c["reviewed"] for c in curves], axis=0),
                    "found": np.sum([c["found"] for c in curves], axis=0),
                }
            )
        n = np.array([g["n_epochs"] for g in groups])
        errors = np.array([g["n_errors"] for g in groups])
        checked = np.stack([g["reviewed"] for g in groups])
        found = np.stack([g["found"] for g in groups])
        sampled_errors = errors[draws].sum(axis=1)
        sampled_checked = checked[draws].sum(axis=1)
        sampled_found = found[draws].sum(axis=1)
        totals_found, totals_checked = found.sum(axis=0), checked.sum(axis=0)
        points = []
        for i, budget in enumerate(budgets):
            recall_samples = (
                sampled_found[sampled_errors > 0, i] / sampled_errors[sampled_errors > 0]
            )
            precision_samples = (
                sampled_found[sampled_checked[:, i] > 0, i]
                / sampled_checked[sampled_checked[:, i] > 0, i]
            )
            points.append(
                {
                    "budget_fraction": budget,
                    "actual_review_fraction": float(totals_checked[i] / n.sum()),
                    "reviewed_epochs": int(totals_checked[i]),
                    "errors_found": int(totals_found[i]),
                    "error_recall": float(totals_found[i] / errors.sum()) if errors.sum() else None,
                    "precision": float(totals_found[i] / totals_checked[i])
                    if totals_checked[i]
                    else None,
                    "error_recall_ci95": np.quantile(recall_samples, [0.025, 0.975]).tolist()
                    if len(recall_samples)
                    else None,
                    "precision_ci95": np.quantile(precision_samples, [0.025, 0.975]).tolist()
                    if len(precision_samples)
                    else None,
                }
            )
        area = (
            float(
                np.trapezoid(
                    [p["error_recall"] for p in points],
                    [p["actual_review_fraction"] for p in points],
                )
            )
            if errors.sum()
            else None
        )
        output[method] = {
            "n_epochs": int(n.sum()),
            "n_errors": int(errors.sum()),
            "area_under_review_recall": area,
            "curve": points,
        }
    if len({tuple(p["reviewed_epochs"] for p in m["curve"]) for m in output.values()}) != 1:
        raise ValueError("Comparison methods do not share identical review counts")
    return {
        "methods": output,
        "bootstrap": {
            "unit": "participant, with all nights kept together",
            "subjects": subjects,
            "samples": bootstrap_samples,
            "seed": seed,
            "interval": "95% percentile; conditional on fixed development predictions and ordering",
        },
        "budget_policy": "ceil(fraction * eligible epochs) independently per recording; pooled counts",
        "interpretation": "reference-error discovery simulation, not measured expert time savings",
    }
