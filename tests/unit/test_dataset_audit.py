"""Offline audit invariants, including data leakage and corrupted downloads."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.audit_sleep_edf import (
    BASE,
    fetch,
    inventory,
    parse_checksums,
    safe_path,
    select_samples,
    sha256,
    split_subjects,
    verify_samples,
)


class AuditTests(unittest.TestCase):
    def test_reject_bad_and_duplicate_checksums(self):
        for text in ("", "garbage", "a" * 64 + " ../outside", ("a" * 64 + " file\n") * 2):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_checksums(text)

    def test_reject_unsafe_paths(self):
        for name in ("../outside", "/absolute", "C:/secret", "x\\y", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_path(Path("data"), name)

    def test_pair_by_recording_not_scorer(self):
        psg = "sleep-cassette/SC4001E0-PSG.edf"
        hyp = "sleep-cassette/SC4001EC-Hypnogram.edf"
        metadata = {("SC", 0, 1): {"metadata_row": 2}}
        records = inventory({psg: "a" * 64, hyp: "b" * 64}, metadata)
        self.assertEqual(records[0]["subject_id"], "SC-00")
        self.assertEqual(records[0]["annotation"], hyp)
        with self.assertRaises(ValueError):
            inventory({psg: "a" * 64}, metadata)
        with self.assertRaises(ValueError):
            inventory({psg: "a" * 64, hyp: "b" * 64}, {})
        with self.assertRaises(ValueError):
            inventory({psg: "a" * 64, hyp: "b" * 64, hyp.replace("EC-", "ED-"): "c" * 64}, metadata)

    @staticmethod
    def records():
        return [{"cohort": cohort, "subject_id": f"{cohort}-{subject:02d}", "recording_id": f"{cohort}-{subject}-{night}"} for cohort in ("SC", "ST") for subject in range(20) for night in (1, 2)]

    def test_subject_split_stable_and_disjoint(self):
        records = self.records()
        splits = split_subjects(records)
        self.assertEqual(splits, split_subjects(list(reversed(records))))
        self.assertNotEqual(splits, split_subjects(records, seed=43))
        self.assertEqual(len(splits), 40)
        for cohort in ("SC", "ST"):
            labels = [v for k, v in splits.items() if k.startswith(cohort)]
            self.assertEqual([labels.count(s) for s in ("train", "validation", "test")], [14, 3, 3])
        for subject in splits:
            self.assertEqual(len({splits[r["subject_id"]] for r in records if r["subject_id"] == subject}), 1)

    def test_sample_selection_never_touches_test(self):
        records = self.records()
        splits = split_subjects(records)
        samples = select_samples(records, splits)
        self.assertEqual(len(samples), 5)
        self.assertTrue(all(splits[r["subject_id"]] == "train" for r in samples))
        with self.assertRaises(ValueError):
            split_subjects(records[:2])

    def test_corruption_fails_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "x.edf").write_bytes(b"data")
            expected = hashlib.sha256(b"data").hexdigest()
            self.assertEqual(sha256(root / "x.edf"), expected)
            with patch("tools.audit_sleep_edf.urlopen") as network:
                self.assertEqual(fetch(root, "x.edf", expected), root.resolve() / "x.edf")
                with self.assertRaises(ValueError):
                    fetch(root, "x.edf", "0" * 64)
                network.assert_not_called()
            record = {"psg": "x.edf", "annotation": "x.edf", "psg_sha256": expected, "annotation_sha256": expected}
            self.assertEqual(verify_samples(root, [record]), 8)
            (root / "x.edf").write_bytes(b"corrupted")
            with self.assertRaises(ValueError):
                verify_samples(root, [record])

    def test_bad_download_removed_and_not_promoted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("tools.audit_sleep_edf.urlopen") as network:
                source = network.return_value.__enter__.return_value
                source.geturl.return_value = BASE + "x.edf"
                source.read.side_effect = [b"bad", b""]
                with self.assertRaises(ValueError):
                    fetch(root, "x.edf", "0" * 64)
            self.assertFalse((root / "x.edf").exists())
            self.assertFalse((root / "x.edf.part").exists())

    def test_untrusted_redirect_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("tools.audit_sleep_edf.urlopen") as network:
                source = network.return_value.__enter__.return_value
                source.geturl.return_value = "https://example.com/x.edf"
                with self.assertRaises(ValueError):
                    fetch(Path(directory), "x.edf")

    def test_orphan_and_cohort_conflict_rejected(self):
        metadata = {("SC", 0, 1): {}}
        files = {"sleep-cassette/SC4001E0-PSG.edf": "a" * 64,
                 "sleep-cassette/SC4001EC-Hypnogram.edf": "b" * 64,
                 "sleep-cassette/SC4002EC-Hypnogram.edf": "c" * 64}
        with self.assertRaises(ValueError):
            inventory(files, metadata)
        with self.assertRaises(ValueError):
            inventory({"sleep-telemetry/SC4001E0-PSG.edf": "a" * 64}, metadata)


if __name__ == "__main__":
    unittest.main()
