"""Local CLI import; no model inference, network, or database side effects."""

import argparse
import json
import logging
import re
from pathlib import Path

from packages.signals.pipeline import import_recording
from tools.audit_sleep_edf import MANIFEST_SHA256, parse_checksums, safe_path, sha256

LOGGER = logging.getLogger("hypnoreview.import")


def write_result(path: Path, result: dict) -> None:
    text = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise ValueError("Existing import differs; use a new output directory")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.part")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--psg", type=Path)
    mode.add_argument(
        "--samples", action="store_true", help="Import the five frozen training samples"
    )
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--manifest", type=Path, default=Path("data/audit/manifest.json"))
    parser.add_argument("--output", type=Path, default=Path("derived/imports"))
    parser.add_argument("--eeg")
    parser.add_argument("--eog")
    parser.add_argument("--emg")
    args = parser.parse_args(argv)
    if args.samples and (args.annotations or args.eeg or args.eog or args.emg):
        parser.error("Per-recording annotations/mapping cannot be combined with --samples")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        jobs = []
        if args.samples:
            root = args.manifest.parent
            checksum_path = root / "SHA256SUMS.txt"
            if sha256(checksum_path) != MANIFEST_SHA256:
                raise ValueError("Unrecognized source checksum manifest")
            checksums = parse_checksums(checksum_path.read_text())
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            ids = manifest["sample_recording_ids"]
            if len(set(ids)) != 5:
                raise ValueError("Expected five unique development samples")
            for record in manifest["recordings"]:
                if record["recording_id"] not in ids:
                    continue
                if manifest["subject_splits"][record["subject_id"]] != "train":
                    raise ValueError("Sample import refuses validation/test participants")
                for kind in ("psg", "annotation"):
                    if checksums[record[kind]] != sha256(safe_path(root, record[kind])):
                        raise ValueError("Sample checksum mismatch")
                jobs.append(
                    (
                        record["recording_id"],
                        safe_path(root, record["psg"]),
                        safe_path(root, record["annotation"]),
                        {
                            "subject_id": record["subject_id"],
                            "study_id": "sleep-edfx-1.0.0",
                            "split": "train",
                            "audit_manifest_sha256": sha256(args.manifest),
                        },
                    )
                )
            if len(jobs) != 5:
                raise ValueError("Manifest sample IDs do not match five recordings")
        else:
            jobs.append((args.psg.stem, args.psg, args.annotations, {}))
        mapping = {"eeg": args.eeg, "eog": args.eog, "emg": args.emg} if args.eeg else None
        if not args.eeg and (args.eog or args.emg):
            raise ValueError("Explicit optional channels require --eeg")
        failures = 0
        for identifier, psg, annotations, provenance in jobs:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
                raise ValueError(
                    "Recording identifier must contain only letters, digits, hyphens and underscores"
                )
            try:
                result = import_recording(psg, annotations, mapping)
                result["identity"] = {"recording_id": identifier, **provenance}
                write_result(args.output / f"{identifier}.json", result)
                LOGGER.info(
                    "import_succeeded recording=%s epochs=%d valid_reference=%d",
                    identifier,
                    len(result["epochs"]),
                    sum(e["valid_reference"] for e in result["epochs"]),
                )
            except (OSError, ValueError, RuntimeError) as error:
                failures += 1
                LOGGER.error("import_failed recording=%s reason=%s", identifier, error)
        return int(failures > 0)
    except (OSError, ValueError, KeyError, TypeError) as error:
        LOGGER.error("import_setup_failed: %s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
