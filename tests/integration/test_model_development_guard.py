"""The developer command must reject frozen held-out participants before I/O."""

import json
from unittest.mock import patch

import pytest

from packages.models.cli import development_inputs
from tools.audit_sleep_edf import MANIFEST_SHA256


@pytest.mark.parametrize("split", ["validation", "test"])
def test_held_out_subjects_refused(tmp_path, split):
    ids = ["sample" + str(i) for i in range(5)]
    manifest = {
        "sample_recording_ids": ids,
        "recordings": [{"recording_id": ids[0], "subject_id": "SC-00"}],
        "subject_splits": {"SC-00": split},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    (tmp_path / "SHA256SUMS.txt").write_text("a" * 64 + " source.edf\n")
    with patch("packages.models.cli.sha256", return_value=MANIFEST_SHA256):
        with pytest.raises(ValueError, match="refuses validation and test"):
            development_inputs(path, tmp_path)
