"""Bounded native-rate signal reads and visualization-only aggregates."""

from math import ceil
from pathlib import Path

import numpy as np
from scipy.signal import spectrogram

from packages.signals.edf_loader import EDFRecording


def window(
    path: Path,
    start: float,
    duration: float,
    channels: list[str],
    points: int,
    spectral: bool = False,
) -> dict:
    with EDFRecording(path) as recording:
        stop = min(start + duration, recording.header.duration_s)
        if start >= stop:
            raise ValueError("Window starts beyond recording")
        if len(set(channels)) != len(channels) or not 1 <= len(channels) <= 3:
            raise ValueError("Select one to three distinct voltage channels")
        output = []
        for name in channels:
            channel = recording.channels.get(name)
            if channel is None or channel.sample_rate_hz * (stop - start) > 1_000_000:
                raise ValueError("Unknown channel or window exceeds sample limit")
            pieces = []
            cursor = start
            while cursor < stop - 1e-8:
                length = min(30.0, stop - cursor)
                pieces.append(recording.read_channel(name, cursor, length))
                cursor += length
            values = np.concatenate(pieces) * 1e6
            if not np.isfinite(values).all():
                raise ValueError("Nonfinite signal cannot be displayed")
            fs = channel.sample_rate_hz
            actual_start = ceil(start * fs - 1e-8) / fs
            row = {
                "channel": name,
                "unit": "uV",
                "native_rate_hz": fs,
                "start_s": actual_start,
                "end_s": stop,
                "filtered": False,
            }
            if spectral:
                if len(values) < 4 or fs < 2:
                    raise ValueError("Insufficient samples/rate for spectrogram")
                size = min(len(values), max(4, int(fs * 2)))
                frequencies, times, power = spectrogram(
                    values, fs=fs, nperseg=size, noverlap=size // 2
                )
                keep = frequencies <= min(35, fs / 2)
                row.update(
                    {
                        "frequencies_hz": frequencies[keep].tolist(),
                        "times_s": (times + actual_start).tolist(),
                        "power_db_uv2_hz": (10 * np.log10(np.maximum(power[keep], 1e-12))).tolist(),
                        "method": "scipy PSD, up to 2s Tukey default windows, 50% overlap; display only",
                    }
                )
            else:
                bins = np.array_split(values, min(points, len(values)))
                offsets = np.r_[0, np.cumsum([len(b) for b in bins])]
                row.update(
                    {
                        "bin_start_s": (actual_start + offsets[:-1] / fs).tolist(),
                        "bin_end_s": (actual_start + offsets[1:] / fs).tolist(),
                        "min_uv": [float(b.min()) for b in bins],
                        "max_uv": [float(b.max()) for b in bins],
                        "display_bins_per_s": len(bins) / (stop - start),
                        "representation": "min_max_envelope",
                    }
                )
            output.append(row)
        return {"start_s": start, "duration_s": stop - start, "channels": output}
