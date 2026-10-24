"""Real MNE decoding of synthetic EDF/EDF+ fixtures, no external datasets."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import mne
import numpy as np

from packages.signals.cli import main
from packages.signals.edf_loader import EDFRecording, load_annotations, read_header
from packages.signals.pipeline import import_recording


def write_edf(path, *, start=datetime(2020, 1, 1), seconds=65, annotations=None):
    def field(value, width):
        return str(value).encode("ascii").ljust(width, b" ")

    if annotations is None:
        channels = [("EEG Fpz-Cz", 100, "uV"), ("EMG submental", 1, "uV")]
        records, record_duration = seconds, 1
        blocks = []
        for second in range(seconds):
            blocks.append(np.tile(np.array([-100, 100], dtype="<i2"), 50).tobytes())
            blocks.append(np.array([second % 10], dtype="<i2").tobytes())
        data = b"".join(blocks)
        reserved = ""
    else:
        tal = b"0\x14\x14\x00" + b"".join(
            f"{onset:+g}\x15{duration:g}\x14{label}\x14\x00".encode()
            for onset, duration, label in annotations
        )
        if len(tal) % 2:
            tal += b"\x00"
        channels = [("EDF Annotations", len(tal) // 2, "")]
        records, record_duration = 1, 0
        data, reserved = tal, "EDF+C"
    n = len(channels)
    fixed = b"".join(
        [
            field("0", 8),
            field("anonymous", 80),
            field("test", 80),
            field(start.strftime("%d.%m.%y"), 8),
            field(start.strftime("%H.%M.%S"), 8),
            field(256 * (n + 1), 8),
            field(reserved, 44),
            field(records, 8),
            field(record_duration, 8),
            field(n, 4),
        ]
    )
    columns = [
        [c[0] for c in channels],
        [""] * n,
        [c[2] for c in channels],
        [-1000] * n,
        [1000] * n,
        [-32768] * n,
        [32767] * n,
        [""] * n,
        [c[1] for c in channels],
        [""] * n,
    ]
    header = b"".join(
        field(v, width)
        for column, width in zip(columns, (16, 80, 8, 8, 8, 8, 8, 80, 8, 32))
        for v in column
    )
    path.write_bytes(fixed + header + data)


class EDFTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.psg = self.root / "synthetic.edf"
        self.hyp = self.root / "annotations.edf"
        write_edf(self.psg)

    def tearDown(self):
        self.temp.cleanup()

    def test_native_rates_voltage_and_bounded_read(self):
        with EDFRecording(self.psg) as recording:
            eeg = recording.read_channel("EEG Fpz-Cz", 0, 30)
            emg = recording.read_channel("EMG submental", 0, 30)
            self.assertEqual(eeg.shape, (3000,))
            self.assertEqual(emg.shape, (30,))
            expected = ((np.array([-100, 100]) + 32768) * 2000 / 65535 - 1000) * 1e-6
            np.testing.assert_allclose(eeg[:2], expected, atol=1e-15)
            self.assertFalse(recording._readers["EEG Fpz-Cz"].preload)
            with self.assertRaises(ValueError):
                recording.read_channel("EEG Fpz-Cz", 0, 31)
            with self.assertRaises(ValueError):
                recording.read_channel("unknown", 0, 30)

    def test_shifted_origin_and_source_preservation(self):
        write_edf(
            self.hyp,
            start=datetime(2020, 1, 1) + timedelta(seconds=30),
            annotations=[(0, 30, "Sleep stage 2")],
        )
        with EDFRecording(self.psg) as recording:
            labels, summary = load_annotations(recording, self.hyp)
        self.assertEqual(summary["offset_s"], 30)
        self.assertEqual(labels[0].start_s, 30)
        original = self.hyp.read_bytes()
        result = import_recording(self.psg, self.hyp)
        self.assertFalse(result["epochs"][0]["valid_reference"])
        self.assertEqual(result["epochs"][1]["reference_stage"], "N2")
        self.assertTrue(result["epochs"][1]["valid_reference"])
        self.assertFalse(result["epochs"][-1]["complete"])
        self.assertEqual(original, self.hyp.read_bytes())

    def test_unannotated_recording_and_cli_idempotence(self):
        args = ["--psg", str(self.psg), "--output", str(self.root / "out")]
        self.assertEqual(main(args), 0)
        self.assertEqual(main(args), 0)
        result = json.loads((self.root / "out/synthetic.json").read_text())
        self.assertFalse(any(e["valid_reference"] for e in result["epochs"]))
        self.assertFalse(result["processing"]["filtered"])

    def test_nonoverlap_truncation_and_discontinuity_rejected(self):
        write_edf(self.hyp, start=datetime(2020, 1, 2), annotations=[(0, 30, "Sleep stage 2")])
        with EDFRecording(self.psg) as recording, self.assertRaises(ValueError):
            load_annotations(recording, self.hyp)
        content = self.psg.read_bytes()
        self.psg.write_bytes(content[:-1])
        with self.assertRaises(ValueError):
            read_header(self.psg)
        self.psg.write_bytes(content[:192] + b"EDF+D" + content[197:])
        with self.assertRaises(ValueError):
            read_header(self.psg)

    def test_explicit_annotation_origin_takes_precedence(self):
        write_edf(self.hyp, annotations=[(0, 30, "Sleep stage 2")])
        annotations = mne.Annotations(
            [5],
            [30],
            ["Sleep stage 2"],
            orig_time=datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=10),
        )
        with EDFRecording(self.psg) as recording:
            with patch(
                "packages.signals.edf_loader.mne.read_annotations", return_value=annotations
            ):
                rows, summary = load_annotations(recording, self.hyp)
        self.assertEqual(rows[0].start_s, 15)
        self.assertEqual(summary["origin_source"], "mne_orig_time")

    def test_unknown_mapping_missing_channel_and_units_rejected(self):
        with self.assertRaises(ValueError):
            import_recording(self.psg, mapping={"eeg": "not-present"})
        with self.assertRaises(ValueError):
            import_recording(self.psg, mapping={"eeg": "EEG Fpz-Cz", "emg": "EEG Fpz-Cz"})
        content = bytearray(self.psg.read_bytes())
        # Physical dimensions follow labels and transducer fields for two channels.
        content[256 + 2 * (16 + 80) : 256 + 2 * (16 + 80) + 8] = b"unknown "
        self.psg.write_bytes(content)
        with EDFRecording(self.psg) as recording, self.assertRaises(ValueError):
            recording.read_channel("EEG Fpz-Cz", 0, 30)
