"""Lazy MNE EDF reads with original sampling metadata and explicit time origins."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import ceil, isclose, isfinite
from pathlib import Path

import mne
import numpy as np
from numpy.typing import NDArray

from packages.core.annotations import Annotation


@dataclass(frozen=True)
class Channel:
    name: str
    unit: str
    sample_rate_hz: float
    sample_count: int
    physical_min: float
    physical_max: float
    digital_min: int
    digital_max: int


@dataclass(frozen=True)
class EDFHeader:
    channels: tuple[Channel, ...]
    duration_s: float
    start_time: datetime
    format: str


def read_header(path: Path) -> EDFHeader:
    """Read only nonidentifying metadata; reject truncated/discontinuous EDF.

    EDF dates have no timezone. UTC is used solely as a common arithmetic frame,
    not a claim about the recording's actual timezone. Patient fields are omitted.
    """
    with path.open("rb") as stream:
        fixed = stream.read(256)
        if len(fixed) != 256 or fixed[:8].strip() != b"0":
            raise ValueError("Not a supported EDF/EDF+ header")
        header_bytes = int(fixed[184:192])
        signals = int(fixed[252:256])
        records = int(fixed[236:244])
        record_s = float(fixed[244:252])
        reserved = fixed[192:236].decode("ascii").strip()
        if reserved.startswith("EDF+D"):
            raise ValueError("Discontinuous EDF+D is not supported; explicit gap handling required")
        if not 1 <= signals <= 512 or header_bytes != 256 * (signals + 1):
            raise ValueError("Invalid EDF signal/header dimensions")
        if not isfinite(record_s) or record_s < 0:
            raise ValueError("Invalid EDF record duration")
        date = fixed[168:176].decode("ascii")
        time = fixed[176:184].decode("ascii")
        day, month, year = map(int, date.split("."))
        year += 1900 if year >= 85 else 2000
        hour, minute, second = map(int, time.split("."))
        start = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        block = stream.read(header_bytes - 256)
        if len(block) != header_bytes - 256:
            raise ValueError("Truncated EDF signal headers")
    cursor = 0
    columns = []
    for width in (16, 80, 8, 8, 8, 8, 8, 80, 8, 32):
        columns.append(
            [
                block[cursor + i * width : cursor + (i + 1) * width].decode("latin-1").strip()
                for i in range(signals)
            ]
        )
        cursor += width * signals
    samples = [int(v) for v in columns[8]]
    if any(v <= 0 for v in samples):
        raise ValueError("Invalid EDF samples per record")
    record_bytes = 2 * sum(samples)
    actual_bytes = path.stat().st_size - header_bytes
    if records == -1:
        records, remainder = divmod(actual_bytes, record_bytes)
        if remainder:
            raise ValueError("Truncated EDF with unknown record count")
    if records <= 0 or actual_bytes != records * record_bytes:
        raise ValueError("EDF payload size disagrees with record count")
    channels = []
    names = columns[0]
    if record_s == 0 and names != ["EDF Annotations"]:
        raise ValueError("Zero record duration is supported only for annotation-only EDF+")
    if len(names) != len(set(names)) or any(not n for n in names):
        raise ValueError("Duplicate or empty EDF channel names")
    for i, name in enumerate(names):
        lo, hi = float(columns[3][i]), float(columns[4][i])
        dmin, dmax = int(columns[5][i]), int(columns[6][i])
        if not all(isfinite(v) for v in (lo, hi)) or lo >= hi or dmin >= dmax:
            raise ValueError("Invalid EDF channel calibration")
        channels.append(
            Channel(
                name,
                columns[2][i],
                samples[i] / record_s if record_s else 0.0,
                samples[i] * records,
                lo,
                hi,
                dmin,
                dmax,
            )
        )
    return EDFHeader(tuple(channels), records * record_s, start, reserved or "EDF")


def channel_mapping(channels: tuple[Channel, ...]) -> dict[str, str | None]:
    """Suggest only exact known channel names; never guess unknown montages."""
    names = {c.name for c in channels}
    return {
        "eeg": "EEG Fpz-Cz" if "EEG Fpz-Cz" in names else None,
        "eog": "EOG horizontal" if "EOG horizontal" in names else None,
        "emg": "EMG submental" if "EMG submental" in names else None,
    }


class EDFRecording:
    """Open voltage channels separately, avoiding mixed-rate MNE upsampling."""

    def __init__(self, path: Path):
        self.path = Path(path)
        if self.path.suffix.lower() != ".edf":
            raise ValueError("Only EDF/EDF+ input is supported")
        self.header = read_header(self.path)
        self.channels = {c.name: c for c in self.header.channels if c.name != "EDF Annotations"}
        if not self.channels:
            raise ValueError("No signal channels in recording")
        self._readers: dict = {}

    def __enter__(self) -> EDFRecording:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        for reader in self._readers.values():
            reader.close()
        self._readers.clear()

    def read_channel(self, name: str, start_s: float, duration_s: float) -> NDArray[np.float64]:
        if name not in self.channels:
            raise ValueError(f"Unknown channel: {name}")
        if (
            not all(isfinite(v) for v in (start_s, duration_s))
            or start_s < 0
            or not 0 < duration_s <= 30
        ):
            raise ValueError("Read windows must be finite, nonnegative and at most 30 seconds")
        if start_s + duration_s > self.header.duration_s + 1e-8:
            raise ValueError("Read window exceeds recording")
        channel = self.channels[name]
        if channel.unit.lower() not in ("uv", "µv", "μv", "mv", "v"):
            raise ValueError("Voltage units required; unknown channel units are not guessed")
        if name not in self._readers:
            self._readers[name] = mne.io.read_raw_edf(
                self.path, include=[name], preload=False, infer_types=False, verbose="ERROR"
            )
        raw = self._readers[name]
        if not isclose(raw.info["sfreq"], channel.sample_rate_hz):
            raise ValueError("Native EDF and MNE sampling rates differ")
        start = ceil(start_s * channel.sample_rate_hz - 1e-8)
        stop = ceil((start_s + duration_s) * channel.sample_rate_hz - 1e-8)
        if stop - start > 1_000_000:
            raise ValueError("Window exceeds the one-million-sample memory limit")
        return raw.get_data(start=start, stop=stop)[0]

    def metadata(self) -> dict:
        return {
            "duration_s": self.header.duration_s,
            "start_time": self.header.start_time.isoformat(),
            "time_basis": "EDF wall-clock, timezone unspecified; common UTC arithmetic frame",
            "format": self.header.format,
            "channels": [asdict(c) for c in self.channels.values()],
            "suggested_mapping": channel_mapping(self.header.channels),
        }


def load_annotations(recording: EDFRecording, path: Path) -> tuple[list[Annotation], dict]:
    header = read_header(path)
    raw = mne.read_annotations(path)
    # Standalone EDF annotations commonly expose orig_time=None. The EDF header
    # then supplies the actual origin; do not assume it equals the PSG start.
    origin = raw.orig_time if raw.orig_time is not None else header.start_time
    offset = (origin - recording.header.start_time).total_seconds()
    annotations = [
        Annotation(float(onset) + offset, float(length), str(label))
        for onset, length, label in zip(raw.onset, raw.duration, raw.description)
    ]
    duration = recording.header.duration_s
    if not any(a.start_s < duration and a.end_s > 0 for a in annotations):
        raise ValueError(
            "Annotation intervals do not overlap recording; check time origins/pairing"
        )
    outside = sum(
        max(0.0, min(a.end_s, 0) - a.start_s) + max(0.0, a.end_s - max(a.start_s, duration))
        for a in annotations
    )
    return annotations, {
        "offset_s": offset,
        "origin_source": "mne_orig_time" if raw.orig_time is not None else "edf_header",
        "out_of_bounds_annotation_s": outside,
        "source_count": len(annotations),
    }
