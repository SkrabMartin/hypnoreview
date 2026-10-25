import { useEffect, useRef } from "react";
import type { Envelope, Epoch, Spectrum } from "../api";

const codes: Record<string, number> = { WAKE: 0, REM: 1, N1: 2, N2: 3, N3: 4 };
export const colors: Record<string, string> = {
  WAKE: "#ce944a",
  N1: "#8a99d4",
  N2: "#5595af",
  N3: "#415a91",
  REM: "#b17bba",
  UNSCORED: "#8a94a3",
  UNSCORABLE: "#b46d69",
};

export function Hypnogram({
  epochs,
  duration,
  selected,
  onSelect,
}: {
  epochs: Epoch[];
  duration: number;
  selected?: Epoch;
  onSelect: (e: Epoch) => void;
}) {
  return (
    <svg
      role="img"
      aria-label="Interactive hypnogram; use epoch controls for keyboard selection"
      viewBox="0 0 1000 150"
      className="hypnogram"
      onClick={(event) => {
        const bounds = event.currentTarget.getBoundingClientRect();
        const t =
          Math.max(
            0,
            (((event.clientX - bounds.left) / bounds.width) * 1000 - 40) / 960,
          ) * duration;
        const e = epochs[Math.min(epochs.length - 1, Math.floor(t / 30))];
        if (e) onSelect(e);
      }}
    >
      {Object.entries(codes).map(([s, c]) => (
        <g key={s}>
          <line
            x1="40"
            x2="1000"
            y1={15 + c * 25}
            y2={15 + c * 25}
            stroke="#e8edf0"
          />
          <text x="0" y={20 + c * 25}>
            {s}
          </text>
        </g>
      ))}
      {epochs.map((e) => {
        const stage = e.prediction?.predicted_stage;
        return stage && codes[stage] !== undefined ? (
          <rect
            key={e.epoch_id}
            x={40 + (e.start_s / duration) * 960}
            y={12 + codes[stage] * 25}
            width={Math.max(0.4, (e.duration_s / duration) * 960)}
            height="6"
            fill={colors[stage]}
          />
        ) : null;
      })}
      {selected && (
        <rect
          x={40 + (selected.start_s / duration) * 960}
          y="3"
          width={Math.max(3, (30 / duration) * 960)}
          height="125"
          fill="#202f4c"
          opacity=".22"
        />
      )}
      <text x="40" y="145">
        0 h
      </text>
      <text x="920" y="145">
        {(duration / 3600).toFixed(1)} h
      </text>
    </svg>
  );
}

export function Waveform({
  data,
  start,
  duration,
  selected,
}: {
  data: Envelope[];
  start: number;
  duration: number;
  selected: Epoch;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    canvas.width = 1400;
    canvas.height = Math.max(160, data.length * 135);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    data.forEach((channel, index) => {
      const y = index * 135 + 70;
      const scale = 0.4; // fixed 100 uV = 40 px, not a diagnostic calibration
      ctx.fillStyle = "#f0f5fa";
      ctx.fillRect(
        ((selected.start_s - start) / duration) * 1400,
        index * 135,
        (30 / duration) * 1400,
        130,
      );
      ctx.strokeStyle = "#cdd8e2";
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(1400, y);
      ctx.stroke();
      ctx.strokeStyle = "#2d6278";
      ctx.beginPath();
      channel.bin_start_s.forEach((t, i) => {
        const x = ((t - start) / duration) * 1400;
        ctx.moveTo(
          x,
          y - Math.max(-150, Math.min(150, channel.max_uv[i])) * scale,
        );
        ctx.lineTo(
          x,
          y - Math.max(-150, Math.min(150, channel.min_uv[i])) * scale,
        );
      });
      ctx.stroke();
      if (channel.native_rate_hz <= 2) {
        // Sparse native samples need a connecting trace; a singleton envelope
        // has min == max and otherwise produces an invisible zero-length line.
        ctx.beginPath();
        channel.bin_start_s.forEach((t, i) => {
          const x = ((t - start) / duration) * 1400;
          const value = (channel.min_uv[i] + channel.max_uv[i]) / 2;
          const py = y - Math.max(-150, Math.min(150, value)) * scale;
          if (i === 0) ctx.moveTo(x, py);
          else ctx.lineTo(x, py);
        });
        ctx.stroke();
      }
      ctx.fillStyle = "#263e53";
      ctx.font = "15px sans-serif";
      ctx.fillText(
        `${channel.channel} · ${channel.native_rate_hz} Hz${channel.native_rate_hz < 10 && channel.channel.startsWith("EMG") ? " · low-rate envelope" : ""} · ±150 µV display clip`,
        10,
        index * 135 + 20,
      );
    });
  }, [data, start, duration, selected]);
  return (
    <canvas
      ref={ref}
      aria-label="EEG EOG EMG min/max signal envelope"
      role="img"
      className="waveform"
    />
  );
}

export function Spectrogram({ data }: { data: Spectrum[] }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    const s = data[0];
    if (!canvas || !ctx || !s) return;
    canvas.width = 1400;
    canvas.height = 170;
    ctx.clearRect(0, 0, 1400, 170);
    const values = s.power_db_uv2_hz.flat().sort((a, b) => a - b);
    const lo = values[Math.floor(values.length * 0.05)],
      hi = values[Math.floor(values.length * 0.95)];
    s.power_db_uv2_hz.forEach((row, f) =>
      row.forEach((value, t) => {
        const v = Math.max(0, Math.min(1, (value - lo) / (hi - lo || 1)));
        ctx.fillStyle = `hsl(${230 - v * 200} 55% ${18 + v * 50}%)`;
        ctx.fillRect(
          (t / s.times_s.length) * 1400,
          170 - ((f + 1) / s.frequencies_hz.length) * 170,
          Math.ceil(1400 / s.times_s.length),
          Math.ceil(170 / s.frequencies_hz.length),
        );
      }),
    );
  }, [data]);
  return (
    <div>
      <canvas
        ref={ref}
        role="img"
        aria-label="EEG spectrogram"
        className="spectrogram"
      />
      <small>
        Bottom: 0 Hz · Top: {data[0]?.frequencies_hz.at(-1)?.toFixed(0)} Hz ·
        Color: relative PSD (5–95% range, dB µV²/Hz). Same time window as
        signal.
      </small>
    </div>
  );
}
