"""Generate cross-fitted development queues and equal-budget review comparisons."""

import argparse
import hashlib
import importlib.metadata
import json
import logging
from dataclasses import asdict
from pathlib import Path

import numpy as np

from packages.evaluation.review import aggregate_review, review_curve
from packages.models.baseline import predict_baseline
from packages.models.cli import development_inputs, digest
from packages.models.contracts import STAGES, probabilities
from packages.ranking.context import fit_context, quality_risk, transition_surprise
from packages.ranking.prioritization import order_queue
from packages.ranking.uncertainty import RankingConfig, score_epochs
from packages.signals.cli import write_result
from tools.audit_sleep_edf import sha256

LOGGER = logging.getLogger("hypnoreview.review")


def review_orders(rows: list[dict], radius: int = 2) -> dict[str, list[int]]:
    methods = {
        name: order_queue(rows, name, radius)
        for name in ("strict_priority", "coverage_aware", "random")
    }
    for method, field in (
        ("confidence", "confidence_uncertainty"),
        ("entropy", "entropy"),
        ("stage_boundary", "stage_boundary"),
    ):
        altered = [{**row, "review_score": row[field]} for row in rows]
        methods[method] = order_queue(altered)
    return methods


def build_queue(
    imported: dict, primary_rows: list[dict], features: list, held_model: dict, context: dict
) -> list[dict]:
    # Reference fields are never forwarded to the scorer or ordering functions.
    subject = imported["identity"]["subject_id"]
    if subject in context["train_subjects"] or subject in held_model["train_subjects"]:
        raise ValueError("Reviewed participant must be excluded from context and baseline training")
    if len(primary_rows) != len(imported["epochs"]) or len(features) != len(primary_rows):
        raise ValueError("Imported, primary and feature epoch counts differ")
    usable = [
        i
        for i, row in enumerate(primary_rows)
        if row["status"] == "predicted" and imported["epochs"][i]["complete"]
    ]
    if not usable:
        raise ValueError("No complete primary predictions available")
    for i, row in enumerate(primary_rows):
        if (
            row["epoch_index"] != i
            or row["start_s"] != i * 30
            or imported["epochs"][i]["index"] != i
        ):
            raise ValueError("Epoch identities/time axes are not aligned")
    p = probabilities(
        [[primary_rows[i]["stage_probabilities"][s] for s in STAGES] for i in usable], list(STAGES)
    )
    if [STAGES[int(i)] for i in p.argmax(axis=1)] != [
        primary_rows[i]["predicted_stage"] for i in usable
    ]:
        raise ValueError("Primary labels disagree with probabilities")
    secondary: list = [None] * len(usable)
    indices = [j for j, i in enumerate(usable) if features[i] is not None]
    if indices:
        values = predict_baseline(held_model, [features[usable[j]] for j in indices])
        for j, row in zip(indices, values, strict=True):
            secondary[j] = row
    quality_rows = [{"quality": imported["epochs"][i].get("quality", {})} for i in usable]
    scores = score_epochs(
        p, transition_surprise(p, usable, context), quality_risk(quality_rows, context), secondary
    )
    labels = p.argmax(axis=1)
    output = []
    for j, i in enumerate(usable):
        boundary = float(
            (j > 0 and usable[j - 1] + 1 == i and labels[j - 1] != labels[j])
            or (j + 1 < len(usable) and usable[j + 1] == i + 1 and labels[j + 1] != labels[j])
        )
        output.append(
            {
                "recording_id": imported["identity"]["recording_id"],
                "epoch_index": i,
                "start_s": i * 30,
                "predicted_stage": STAGES[int(labels[j])],
                "stage_probabilities": dict(zip(STAGES, p[j].tolist())),
                **scores[j],
                "confidence_uncertainty": float(1 - p[j].max()),
                "entropy": scores[j]["components"]["entropy"],
                "stage_boundary": boundary,
            }
        )
    return output


def run_review(
    model_run: Path, manifest: Path, imports: Path, output: Path, bootstrap: int = 500
) -> dict:
    jobs = development_inputs(manifest, imports)
    metrics_path = model_run / "metrics.json"
    metrics = json.loads(metrics_path.read_text())
    if (
        metrics["provenance"]["manifest_sha256"] != sha256(manifest)
        or not metrics["provenance"]["with_yasa"]
    ):
        raise ValueError("Review requires a matching Phase 3 run with primary YASA predictions")
    if sha256(model_run / "baseline-model.json") != metrics["baseline_model"]["weight_sha256"]:
        raise ValueError("Phase 3 baseline model checksum mismatch")
    contexts, fold_models = {}, {}
    subjects = {record["subject_id"] for record, _, _ in jobs}
    training = [
        {"subject_id": record["subject_id"], "epochs": imported["epochs"]}
        for record, imported, _ in jobs
    ]
    for fold in metrics["folds"]:
        train, held = set(fold["train_subjects"]), set(fold["held_subjects"])
        if (
            len(held) != 1
            or train & held
            or train | held != subjects
            or set(fold["model_artifact"]["train_subjects"]) != train
        ):
            raise ValueError("Phase 3 fold participant provenance is invalid")
        subject = next(iter(held))
        if subject in contexts:
            raise ValueError("Duplicate held-out fold")
        contexts[subject] = fit_context([r for r in training if r["subject_id"] in train], held)
        fold_models[subject] = fold["model_artifact"]
    if set(contexts) != subjects:
        raise ValueError("Missing held-out participant folds")
    inputs = {"metrics.json": sha256(metrics_path)}
    for record, imported, _ in jobs:
        identifier = record["recording_id"]
        if metrics["provenance"]["import_sha256"][identifier] != sha256(
            imports / f"{identifier}.json"
        ):
            raise ValueError("Import file differs from the Phase 3 run")
        for suffix in ("yasa", "features"):
            path = model_run / f"{identifier}-{suffix}.json"
            inputs[path.name] = sha256(path)
    config = {
        "version": "review-engine-v1",
        "weights": asdict(RankingConfig()),
        "coverage_radius_epochs": 2,
        "random_seed": 42,
        "bootstrap_samples": bootstrap,
        "model_run_id": metrics["run_id"],
        "input_sha256": inputs,
        "manifest_sha256": sha256(manifest),
        "implementation_sha256": hashlib.sha256(
            b"".join(
                p.read_bytes()
                for folder in (Path(__file__).parent, Path(__file__).parents[1] / "evaluation")
                for p in sorted(folder.glob("*.py"))
            )
        ).hexdigest(),
        "numpy_version": np.__version__,
        "package_version": importlib.metadata.version("hypnoreview"),
    }
    identifier = digest(config)
    target = output / identifier
    budgets = [i / 100 for i in range(101)]
    all_rows, record_results = [], []
    for record, imported, _ in jobs:
        name, subject = record["recording_id"], record["subject_id"]
        primary = json.loads((model_run / f"{name}-yasa.json").read_text())
        features = json.loads((model_run / f"{name}-features.json").read_text())
        if (
            primary["run_id"] != metrics["run_id"]
            or primary["source"] != imported["source"]
            or primary["model"]["weight_sha256"] != metrics["provenance"]["yasa_weight_sha256"]
            or features["psg_sha256"] != record["psg_sha256"]
        ):
            raise ValueError("Primary prediction/feature source provenance mismatch")
        rows = build_queue(
            imported,
            primary["epochs"],
            features["features"],
            fold_models[subject],
            contexts[subject],
        )
        orders = review_orders(rows)
        write_result(
            target / f"{name}-queue.json",
            {
                "run_id": identifier,
                "model_run_id": metrics["run_id"],
                "source": imported["source"],
                "primary_model": primary["model"],
                "secondary_model_scope": "held-out participant baseline",
                "ranking_context": contexts[subject],
                "rows": rows,
                "orders": orders,
                "unavailable_epochs": [
                    {"epoch_index": e["index"], "reasons": ["no_complete_primary_prediction"]}
                    for e in imported["epochs"]
                    if e["index"] not in {row["epoch_index"] for row in rows}
                ],
            },
        )
        # References enter only the separate benchmark after scores/orders are fixed.
        eligible_positions = [
            i
            for i, row in enumerate(rows)
            if imported["epochs"][row["epoch_index"]]["valid_reference"]
        ]
        position_map = {pos: i for i, pos in enumerate(eligible_positions)}
        errors = [
            rows[i]["predicted_stage"]
            != imported["epochs"][rows[i]["epoch_index"]]["reference_stage"]
            for i in eligible_positions
        ]
        methods = {
            method: review_curve(
                errors, [position_map[i] for i in order if i in position_map], budgets
            )
            for method, order in orders.items()
        }
        record_results.append({"recording_id": name, "subject_id": subject, "methods": methods})
        all_rows.extend(rows)
        LOGGER.info("queue_created recording=%s epochs=%d", name, len(rows))
    global_orders = {
        method: order_queue(all_rows, method) for method in ("strict_priority", "coverage_aware")
    }
    write_result(
        target / "global-queue.json",
        {"run_id": identifier, "rows": all_rows, "orders": global_orders},
    )
    report = {
        "run_id": identifier,
        "configuration": config,
        "contexts": contexts,
        "records": record_results,
        **aggregate_review(record_results, budgets, bootstrap),
        "limitations": [
            "Three local training participants; cross-fitted development only",
            "Untuned heuristic weights; QC is not clinical severity",
            "No independent locked-test evaluation",
            "YASA pretraining overlap and serialization warnings remain unresolved",
            "Percentile intervals are unstable with three participant clusters",
            "One seeded random order; no random-order Monte Carlo interval",
        ],
    }
    write_result(target / "review-metrics.json", report)
    plot_curves(report, target / "review-curves.png")
    return report


def plot_curves(report: dict, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 6))
    for method, values in report["methods"].items():
        curve = values["curve"]
        x = [p["actual_review_fraction"] for p in curve]
        y = [p["error_recall"] for p in curve]
        if all(v is None for v in y):
            continue
        line = ax.plot(x, y, label=method.replace("_", " "))[0]
        if method == "strict_priority":
            ax.fill_between(
                x,
                [p["error_recall_ci95"][0] for p in curve],
                [p["error_recall_ci95"][1] for p in curve],
                color=line.get_color(),
                alpha=0.15,
                label="strict priority: 95% participant interval",
            )
    ax.set(
        xlabel="Reviewed fraction of eligible epochs (per-night budgets)",
        ylabel="Fraction of reference errors discovered",
        title=f"Development review simulation — {len(report['bootstrap']['subjects'])} participant clusters",
        xlim=(0, 1),
        ylim=(0, 1),
    )
    ax.grid(alpha=0.2)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=150, metadata={"Software": "HypnoReview"})
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-run", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path("data/audit/manifest.json"))
    parser.add_argument("--imports", type=Path, default=Path("derived/imports"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/review"))
    parser.add_argument("--bootstrap-samples", type=int, default=500)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        if args.bootstrap_samples < 1:
            raise ValueError("Positive bootstrap sample count required")
        report = run_review(
            args.model_run, args.manifest, args.imports, args.output, args.bootstrap_samples
        )
        print(
            json.dumps(
                {
                    "run_id": report["run_id"],
                    "at_20_percent": {
                        name: value["curve"][20] for name, value in report["methods"].items()
                    },
                },
                allow_nan=False,
            )
        )
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        LOGGER.error("review_run_failed: %s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
