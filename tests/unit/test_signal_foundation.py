"""Scientific invariants using small signals and exact interval cases."""

import unittest

import numpy as np

from packages.core.annotations import Annotation
from packages.signals.epochs import align_epochs
from packages.signals.quality import QualityConfig, signal_quality


class AlignmentTests(unittest.TestCase):
    def test_boundaries_and_tail(self):
        rows = align_epochs(
            65, [Annotation(0, 30, "Sleep stage W"), Annotation(30, 35, "Sleep stage 2")]
        )
        self.assertEqual([r.reference_stage for r in rows], ["WAKE", "N2", "N2"])
        self.assertEqual([r.valid_reference for r in rows], [True, True, False])
        self.assertEqual(rows[-1].duration_s, 5)
        self.assertEqual(len(align_epochs(60, [])), 2)

    def test_duplicate_annotations_are_union_not_sum(self):
        a = Annotation(0, 15, "Sleep stage 2")
        row = align_epochs(30, [a, a])[0]
        self.assertEqual(row.covered_s, 15)
        self.assertFalse(row.valid_reference)
        row = align_epochs(30, [Annotation(0, 30, "Sleep stage 2")] * 2)[0]
        self.assertTrue(row.valid_reference)

    def test_conflict_invalid_even_with_full_dominant_label(self):
        row = align_epochs(
            30, [Annotation(0, 30, "Sleep stage W"), Annotation(10, 0.01, "Sleep stage 1")]
        )[0]
        self.assertFalse(row.valid_reference)
        self.assertAlmostEqual(row.covered_s, 30)
        self.assertAlmostEqual(row.conflict_s, 0.01)

    def test_historical_mapping_and_exclusions(self):
        for label, stage in [
            ("3", "N3"),
            ("4", "N3"),
            ("M", "ARTIFACT_OR_MOVEMENT"),
            ("?", "UNSCORED"),
        ]:
            row = align_epochs(30, [Annotation(0, 30, "Sleep stage " + label)])[0]
            self.assertEqual(row.reference_stage, stage)
            self.assertEqual(row.valid_reference, stage == "N3")
            self.assertEqual(row.original_labels, ("Sleep stage " + label,))

    def test_mixed_unknown_and_missing(self):
        row = align_epochs(
            30, [Annotation(0, 15, "Sleep stage 3"), Annotation(15, 15, "Sleep stage 4")]
        )[0]
        self.assertIn("mixed_labels", row.reasons)
        self.assertFalse(row.valid_reference)
        row = align_epochs(30, [Annotation(0, 30, "unrecognized")])[0]
        self.assertEqual(row.unknown_s, 30)
        self.assertFalse(row.valid_reference)
        self.assertFalse(align_epochs(30, [])[0].valid_reference)

    def test_coverage_threshold_negative_onset_and_gap(self):
        self.assertTrue(
            align_epochs(30, [Annotation(-1, 30.9, "Sleep stage R")])[0].valid_reference
        )
        self.assertFalse(
            align_epochs(30, [Annotation(0, 29.8, "Sleep stage R")])[0].valid_reference
        )

    def test_reject_nonfinite_and_invalid_duration(self):
        for value in (-1, 0, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                align_epochs(value, [])
        with self.assertRaises(ValueError):
            Annotation(0, -1, "Sleep stage W")
        with self.assertRaises(ValueError):
            align_epochs(30, [], minimum_coverage_s=31)


class QualityTests(unittest.TestCase):
    def test_voltage_conversion_and_input_preserved(self):
        values = np.array([-10, 10], dtype=float) * 1e-6
        original = values.copy()
        qc = signal_quality(values)
        self.assertAlmostEqual(qc["ptp_uv"], 20)
        self.assertAlmostEqual(qc["rms_uv"], 10)
        np.testing.assert_array_equal(values, original)
        self.assertEqual(qc["flags"], [])

    def test_flat_extreme_nonfinite(self):
        self.assertIn("flat_signal", signal_quality(np.zeros(30))["flags"])
        self.assertIn("extreme_amplitude", signal_quality(np.array([0, 1e-3]))["flags"])
        qc = signal_quality(np.array([np.nan, np.inf]))
        self.assertIsNone(qc["ptp_uv"])
        self.assertEqual(qc["nonfinite_count"], 2)
        with self.assertRaises(ValueError):
            signal_quality(np.array([]))
        with self.assertRaises(ValueError):
            QualityConfig(extreme_abs_uv=float("nan"))
