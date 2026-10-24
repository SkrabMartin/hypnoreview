"""Audit Sleep-EDF metadata and file integrity; never decode EEG or labels.

Outputs and downloads stay under the ignored data/ directory by default.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path, PurePosixPath
from urllib.request import urlopen

BASE = "https://physionet.org/files/sleep-edfx/1.0.0/"
S3_BASE = "https://physionet-open.s3.amazonaws.com/sleep-edfx/1.0.0/"
SOURCE = "https://physionet.org/content/sleep-edfx/1.0.0/"
MANIFEST_SHA256 = "1bc88e19e2e921f7851d7cf61e31ceb8f6d670211ca546a323e9db5a77525edf"
LOGGER = logging.getLogger("sleep_edf_audit")


def sha256(path: Path) -> str:
    """Hash in bounded chunks, including large PSG files."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    """Reject absolute paths, traversal, Windows separators, and symlink escapes."""
    parts = PurePosixPath(relative)
    if not relative or parts.is_absolute() or ".." in parts.parts or "\\" in relative or ":" in relative:
        raise ValueError(f"Unsafe source path: {relative!r}")
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError("Path escapes download directory")
    return result


def parse_checksums(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64})\s+\*?(.+)", line)
        if match is None:
            raise ValueError("Malformed SHA256SUMS line")
        digest, name = match.groups()
        safe_path(Path("."), name)
        if name in result:
            raise ValueError(f"Duplicate checksum path: {name}")
        result[name] = digest
    if not result:
        raise ValueError("Empty checksum manifest")
    return result


def fetch(root: Path, name: str, expected: str | None = None, base: str = BASE) -> Path:
    """Download atomically, reuse matching files, reject corrupt existing files."""
    if base not in (BASE, S3_BASE):
        raise ValueError("Unrecognized dataset mirror")
    destination = safe_path(root, name)
    if destination.exists():
        if expected is not None and sha256(destination) != expected:
            raise ValueError(f"Checksum mismatch in existing file: {name}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        with urlopen(base + name, timeout=60) as source, temporary.open("wb") as target:
            if not source.geturl().startswith(base):
                raise ValueError("Download redirected outside the versioned source")
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target.write(chunk)
        if expected is not None and sha256(temporary) != expected:
            raise ValueError(f"Downloaded checksum mismatch: {name}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    LOGGER.info("downloaded %s", name)
    return destination


def read_subject_nights(root: Path) -> dict[tuple[str, int, int], dict]:
    """Cross-check filename identities against both official XLS tables."""
    import xlrd

    result: dict[tuple[str, int, int], dict] = {}

    def add(cohort: str, subject: object, night: object, row: int, condition: str) -> None:
        if not isinstance(subject, (int, float)) or int(subject) != subject:
            raise ValueError("Nonintegral subject identifier")
        if night not in (1, 2):
            raise ValueError("Invalid night in metadata")
        key = (cohort, int(subject), int(night))
        if key in result:
            raise ValueError(f"Duplicate subject/night metadata: {key}")
        result[key] = {"metadata_file": f"{cohort}-subjects.xls", "metadata_row": row, "condition": condition}

    sc = xlrd.open_workbook(str(root / "SC-subjects.xls")).sheet_by_index(0)
    if sc.row_values(0)[:2] != ["subject", "night"]:
        raise ValueError("Unexpected SC table schema")
    for row in range(1, sc.nrows):
        values = sc.row_values(row)
        add("SC", values[0], values[1], row + 1, "unmedicated")
    st = xlrd.open_workbook(str(root / "ST-subjects.xls")).sheet_by_index(0)
    if st.cell_value(1, 0) != "Nr" or st.cell_value(0, 3) != "Placebo night" or st.cell_value(0, 5) != "Temazepam night":
        raise ValueError("Unexpected ST table schema")
    for row in range(2, st.nrows):
        values = st.row_values(row)
        add("ST", values[0], values[3], row + 1, "placebo")
        add("ST", values[0], values[5], row + 1, "temazepam")
    return result


def inventory(checksums: dict[str, str], metadata: dict[tuple[str, int, int], dict]) -> list[dict]:
    records = []
    used: set[tuple[str, int, int]] = set()
    used_annotations: set[str] = set()
    for path in sorted(checksums):
        if not path.endswith("-PSG.edf"):
            continue
        match = re.fullmatch(r"(sleep-cassette|sleep-telemetry)/(SC4|ST7)(\d{2})([12])[A-Z]0-PSG\.edf", path)
        if match is None:
            raise ValueError(f"Unknown recording naming scheme: {path}")
        directory, prefix, subject, night = match.groups()
        cohort = prefix[:2]
        if directory != {"SC": "sleep-cassette", "ST": "sleep-telemetry"}[cohort]:
            raise ValueError("Cohort/directory mismatch")
        key = (cohort, int(subject), int(night))
        if key not in metadata or key in used:
            raise ValueError(f"Missing or duplicate metadata match: {key}")
        # First seven filename characters identify a specific recording; scorer differs.
        stem = PurePosixPath(path).name[:7]
        candidates = [p for p in checksums if p.startswith(directory + "/" + stem) and p.endswith("-Hypnogram.edf")]
        if len(candidates) != 1:
            raise ValueError(f"Expected one annotation file for {path}, got {len(candidates)}")
        annotation = candidates[0]
        used.add(key)
        used_annotations.add(annotation)
        records.append({"recording_id": PurePosixPath(path).name[:8], "subject_id": f"{cohort}-{subject}", "cohort": cohort, "night": int(night), "psg": path, "annotation": annotation, "psg_sha256": checksums[path], "annotation_sha256": checksums[annotation], **metadata[key]})
    if used != set(metadata):
        raise ValueError("Metadata and recording inventory do not cover identical nights")
    if used_annotations != {p for p in checksums if p.endswith("-Hypnogram.edf")}:
        raise ValueError("Unmatched annotations")
    return records


def split_subjects(records: list[dict], seed: int = 42) -> dict[str, str]:
    """Stable SHA-256 ordering, stratified by cohort; no labels used.

    Allocate floor(15% N) each to validation/test and the remainder to train.
    Digest ordering avoids dependence on Python random implementation details.
    """
    result = {}
    for cohort in sorted({r["cohort"] for r in records}):
        subjects = {r["subject_id"] for r in records if r["cohort"] == cohort}
        if len(subjects) < 7:
            raise ValueError("At least seven subjects per cohort required for this split policy")
        ordered = sorted(subjects, key=lambda s: hashlib.sha256(f"{seed}:{s}".encode()).hexdigest())
        held = int(len(ordered) * 0.15)
        for index, subject in enumerate(ordered):
            result[subject] = "test" if index < held else "validation" if index < 2 * held else "train"
    return result


def select_samples(records: list[dict], splits: dict[str, str]) -> list[dict]:
    """Three SC and two ST train nights; include a repeated-night case."""
    selected = []
    for cohort, count in (("SC", 3), ("ST", 2)):
        selected.extend([r for r in records if r["cohort"] == cohort and splits[r["subject_id"]] == "train"][:count])
    if len(selected) != 5:
        raise ValueError("Not enough training recordings for sample selection")
    return selected


def verify_samples(root: Path, samples: list[dict]) -> int:
    total = 0
    for record in samples:
        for kind in ("psg", "annotation"):
            path = safe_path(root, record[kind])
            if not path.is_file() or sha256(path) != record[kind + "_sha256"]:
                raise ValueError(f"Missing or corrupt sample: {record[kind]}")
            total += path.stat().st_size
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/audit"))
    parser.add_argument("--fetch-metadata", action="store_true")
    parser.add_argument("--download-samples", action="store_true")
    parser.add_argument("--verify-samples", action="store_true")
    parser.add_argument("--mirror", choices=("physionet", "s3"), default="physionet")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        base = S3_BASE if args.mirror == "s3" else BASE
        if args.fetch_metadata:
            fetch(args.root, "SHA256SUMS.txt", MANIFEST_SHA256, base)
        if sha256(args.root / "SHA256SUMS.txt") != MANIFEST_SHA256:
            raise ValueError("Source checksum manifest differs from the audited snapshot")
        sums = parse_checksums((args.root / "SHA256SUMS.txt").read_text())
        for name in ("SC-subjects.xls", "ST-subjects.xls"):
            if args.fetch_metadata:
                fetch(args.root, name, sums[name], base)
            if sha256(args.root / name) != sums[name]:
                raise ValueError(f"Metadata checksum mismatch: {name}")
        records = inventory(sums, read_subject_nights(args.root))
        splits = split_subjects(records)
        samples = select_samples(records, splits)
        if args.download_samples:
            for record in samples:
                for kind in ("psg", "annotation"):
                    fetch(args.root, record[kind], record[kind + "_sha256"], base)
        verified_bytes = verify_samples(args.root, samples) if args.verify_samples or args.download_samples else None
        output = {"schema_version": 1, "source": SOURCE, "license": "ODC-By-1.0", "seed": 42, "split_policy": "cohort-stratified-sha256-order-v1", "source_sha256": {name: sha256(args.root / name) for name in ("SHA256SUMS.txt", "SC-subjects.xls", "ST-subjects.xls")}, "recordings": records, "subject_splits": splits, "sample_recording_ids": [r["recording_id"] for r in samples]}
        serialized = json.dumps(output, indent=2, sort_keys=True) + "\n"
        manifest = args.root / "manifest.json"
        if manifest.exists() and manifest.read_text(encoding="utf-8") != serialized:
            raise ValueError("Frozen audit manifest differs; preserve it and review a new version separately")
        manifest.write_text(serialized, encoding="utf-8")
        print(json.dumps({"recordings": dict(Counter(r["cohort"] for r in records)), "subjects": len(splits), "subject_splits": dict(Counter(splits.values())), "samples": output["sample_recording_ids"], "verified_sample_bytes": verified_bytes, "manifest_sha256": sha256(args.root / "manifest.json")}, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, ImportError) as error:
        LOGGER.error("audit_failed: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
