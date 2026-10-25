export const stages = [
  "WAKE",
  "N1",
  "N2",
  "N3",
  "REM",
  "UNSCORED",
  "UNSCORABLE",
] as const;
export type Stage = (typeof stages)[number];
export type Prediction = {
  predicted_stage: Stage;
  stage_probabilities: Record<string, number>;
  review_score: number;
  review_reasons: string[];
  explanation: string;
};
export type Epoch = {
  epoch_id: string;
  epoch_index: number;
  start_s: number;
  duration_s: number;
  complete: boolean;
  revision: number;
  final_reviewed_stage: Stage | null;
  prediction: Prediction | null;
  reference_stage?: string;
};
export type Page<T> = { items: T[]; total: number; analysis_run_id?: string };
export type Recording = {
  id: string;
  subject_id: string;
  duration_s: number;
  sha256: string;
  channels: { name: string; unit: string; sample_rate_hz: number }[];
  suggested_mapping: Record<string, string | null>;
  analysis_runs: { id: string; model_id: string; status: string }[];
};
export type Envelope = {
  channel: string;
  native_rate_hz: number;
  bin_start_s: number[];
  min_uv: number[];
  max_uv: number[];
};
export type Spectrum = {
  channel: string;
  frequencies_hz: number[];
  times_s: number[];
  power_db_uv2_hz: number[][];
};
export type History = {
  revision: number;
  stage: Stage;
  note: string;
  reviewer_id: string;
  created_at: string;
};

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`/api/v1${path}`, init);
  if (!r.ok) {
    const message =
      r.status === 409
        ? "Revision conflict. Reload this epoch and review the latest decision before saving again."
        : `Request failed (${r.status}). Check the API and selected recording.`;
    throw new Error(message);
  }
  const value: unknown = await r.json();
  if (!value || typeof value !== "object")
    throw new Error("Invalid API response");
  const obj = value as Record<string, unknown>;
  if (
    "items" in obj &&
    (!Array.isArray(obj.items) ||
      ("total" in obj && !Number.isInteger(obj.total)))
  )
    throw new Error("Invalid API page");
  if (path.includes("/epochs?") && Array.isArray(obj.items)) {
    for (const raw of obj.items) {
      const e = raw as Epoch;
      if (
        typeof e.epoch_id !== "string" ||
        !Number.isInteger(e.epoch_index) ||
        !Number.isInteger(e.revision) ||
        !Number.isFinite(e.start_s)
      )
        throw new Error("Invalid epoch response");
      if (e.prediction) {
        const p = e.prediction;
        const values = ["WAKE", "N1", "N2", "N3", "REM"].map(
          (s) => p.stage_probabilities[s],
        );
        if (
          !stages.slice(0, 5).includes(p.predicted_stage) ||
          !values.every((v) => Number.isFinite(v) && v >= 0 && v <= 1) ||
          Math.abs(values.reduce((a, b) => a + b, 0) - 1) > 1e-4 ||
          !Number.isFinite(p.review_score) ||
          p.review_score < 0 ||
          p.review_score > 1
        )
          throw new Error("Invalid prediction response");
      }
    }
  }
  return value as T;
}

export async function allEpochs(id: string, run: string): Promise<Epoch[]> {
  const result: Epoch[] = [];
  for (let offset = 0; ; offset += 500) {
    const page = await api<Page<Epoch>>(
      `/recordings/${id}/epochs?run_id=${encodeURIComponent(run)}&offset=${offset}&limit=500`,
    );
    if (!Array.isArray(page.items) || !Number.isInteger(page.total))
      throw new Error("Invalid epoch page");
    result.push(...page.items);
    if (result.length >= page.total) return result;
    if (!page.items.length) throw new Error("Incomplete epoch pagination");
  }
}
