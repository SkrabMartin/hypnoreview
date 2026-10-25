import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { allEpochs, api, stages } from "./api";
import type {
  Epoch,
  Envelope,
  History,
  Page,
  Recording,
  Spectrum,
  Stage,
} from "./api";
import { Hypnogram, Spectrogram, Waveform, colors } from "./components/Plots";
import "./style.css";

const client = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
});

function App() {
  const [id, setId] = useState("");
  const recordings = useQuery({
    queryKey: ["recordings"],
    queryFn: () => api<Page<Recording>>("/recordings"),
  });
  useEffect(() => {
    if (!id && recordings.data?.items[0]) setId(recordings.data.items[0].id);
  }, [recordings.data, id]);
  return (
    <>
      <header>
        <div className="brand">
          <span className="mark">H</span>
          <div>
            HypnoReview <span className="version">RESEARCH WORKSPACE</span>
          </div>
        </div>
        <span className="local">● Local · single user</span>
      </header>
      <div className="notice">
        Research and education only. Predictions are suggestions, not medical
        diagnoses. Human review is required.
      </div>
      <main>
        <div className="page-heading">
          <div>
            <p className="eyebrow">SLEEP STAGING / HUMAN REVIEW</p>
            <h1>Review the uncertain.</h1>
            <p>Inspect the signal. Make a decision. Preserve the evidence.</p>
          </div>
          <label>
            Recording
            <select
              aria-label="Recording"
              value={id}
              onChange={(e) => setId(e.target.value)}
            >
              {recordings.data?.items.map((r) => (
                <option key={r.id}>{r.id}</option>
              ))}
            </select>
          </label>
        </div>
        {recordings.isLoading && <p>Connecting to local API…</p>}
        {recordings.error && <p role="alert">{recordings.error.message}</p>}
        {recordings.data?.items.length === 0 && (
          <p>
            No recordings registered. Run the documented migration and
            registration CLI first.
          </p>
        )}
        {id && <Reviewer key={id} id={id} />}
      </main>
      <footer>
        HypnoReview · Historical R&amp;K reference conventions · Sleep-EDF
        Expanded / PhysioNet · Research prototype
      </footer>
    </>
  );
}

function Reviewer({ id }: { id: string }) {
  const qc = useQueryClient();
  const recording = useQuery({
    queryKey: ["recording", id],
    queryFn: () => api<Recording>(`/recordings/${id}`),
  });
  const [run, setRun] = useState("");
  useEffect(() => {
    if (!run && recording.data?.analysis_runs[0])
      setRun(recording.data.analysis_runs[0].id);
  }, [recording.data, run]);
  const epochs = useQuery({
    queryKey: ["epochs", id, run],
    queryFn: () => allEpochs(id, run),
    enabled: !!run,
  });
  const [order, setOrder] = useState("strict_priority");
  const [offset, setOffset] = useState(0);
  const [pending, setPending] = useState(false);
  const queue = useQuery({
    queryKey: ["queue", id, run, order, offset, pending],
    queryFn: () =>
      api<Page<Epoch>>(
        `/recordings/${id}/epochs?run_id=${encodeURIComponent(run)}&order=${order}&offset=${offset}&limit=20&pending=${pending}`,
      ),
    enabled: !!run,
  });
  const [selectedId, setSelectedId] = useState("");
  useEffect(() => {
    if (!selectedId && queue.data?.items[0])
      setSelectedId(queue.data.items[0].epoch_id);
  }, [queue.data, selectedId]);
  const selected = epochs.data?.find((e) => e.epoch_id === selectedId);
  const [stage, setStage] = useState<Stage>("N2");
  const [note, setNote] = useState("");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    setStage(
      selected?.final_reviewed_stage ||
        selected?.prediction?.predicted_stage ||
        "UNSCORED",
    );
    setNote("");
    setMessage("");
  }, [selectedId, selected?.revision]);
  const history = useQuery({
    queryKey: ["history", selectedId],
    queryFn: () =>
      api<Page<History>>(`/epochs/${encodeURIComponent(selectedId)}/history`),
    enabled: !!selectedId,
  });
  const [context, setContext] = useState(150);
  const start = Math.max(
    0,
    (selected?.start_s || 0) - (context === 30 ? 0 : 60),
  );
  const duration = Math.min(
    context,
    Math.max(0, (recording.data?.duration_s || 0) - start),
  );
  const channels = Object.values(recording.data?.suggested_mapping || {})
    .filter((v): v is string => !!v)
    .join(",");
  const params = `start_s=${start}&duration_s=${duration}&channels=${encodeURIComponent(channels)}`;
  const signal = useQuery({
    queryKey: ["signal", id, params],
    queryFn: () =>
      api<{ channels: Envelope[] }>(
        `/recordings/${id}/signal?${params}&target_points=1400`,
      ),
    enabled: !!selected && !!channels && duration > 0,
  });
  const eeg = recording.data?.suggested_mapping.eeg;
  const spectrum = useQuery({
    queryKey: ["spectrum", id, start, duration, eeg],
    queryFn: () =>
      api<{ channels: Spectrum[] }>(
        `/recordings/${id}/spectrogram?start_s=${start}&duration_s=${duration}&channels=${encodeURIComponent(eeg || "")}`,
      ),
    enabled: !!selected && !!eeg && duration >= 2,
  });
  async function save() {
    if (!selected) return;
    setSaving(true);
    setMessage("");
    try {
      await api(`/epochs/${encodeURIComponent(selectedId)}/review`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          stage,
          note,
          expected_revision: selected.revision,
          analysis_run_id: run,
        }),
      });
      await Promise.all(
        ["epochs", "queue", "history"].map((key) =>
          qc.invalidateQueries({ queryKey: [key] }),
        ),
      );
      setMessage(
        "Revision saved. Original reference and prediction preserved.",
      );
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setSaving(false);
    }
  }
  async function download(format: string) {
    try {
      const r = await fetch(
        `/api/v1/recordings/${id}/exports?run_id=${encodeURIComponent(run)}&format=${format}`,
        { method: "POST" },
      );
      if (!r.ok) throw new Error("Export failed");
      const url = URL.createObjectURL(await r.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download = `${id}-review.${format}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setMessage((e as Error).message);
    }
  }
  const failures = [
    recording.error,
    epochs.error,
    queue.error,
    signal.error,
    spectrum.error,
    history.error,
  ].filter(Boolean);
  return (
    <>
      {failures.map((e, i) => (
        <p role="alert" key={i}>
          {e?.message}
        </p>
      ))}
      <div className="toolbar">
        <span>
          {recording.data?.subject_id} ·{" "}
          {((recording.data?.duration_s || 0) / 3600).toFixed(1)} hours ·{" "}
          {epochs.data?.length || 0} epochs
        </span>
        <label>
          Analysis
          <select
            aria-label="Analysis run"
            value={run}
            onChange={(e) => {
              setRun(e.target.value);
              setOffset(0);
            }}
          >
            {recording.data?.analysis_runs.map((r) => (
              <option key={r.id} value={r.id}>
                {r.status} · {r.id.slice(-12)}
              </option>
            ))}
          </select>
        </label>
        <button onClick={() => download("csv")}>Export CSV ↗</button>
        <button onClick={() => download("json")}>Export JSON ↗</button>
      </div>
      <section className="card">
        <div className="section-title">
          <h2>Night overview</h2>
          <span>Model prediction · click to inspect</span>
        </div>
        {epochs.data && recording.data && (
          <Hypnogram
            epochs={epochs.data}
            duration={recording.data.duration_s}
            selected={selected}
            onSelect={(e) => setSelectedId(e.epoch_id)}
          />
        )}
      </section>
      <div className="workspace">
        <aside className="card queue">
          <div className="section-title">
            <h2>Review queue</h2>
            <span>{queue.data?.total || 0}</span>
          </div>
          <select
            aria-label="Queue strategy"
            value={order}
            onChange={(e) => {
              setOrder(e.target.value);
              setOffset(0);
            }}
          >
            <option value="strict_priority">Highest priority</option>
            <option value="coverage_aware">Coverage aware</option>
            <option value="timeline">Chronological</option>
          </select>
          <label className="check">
            <input
              type="checkbox"
              checked={pending}
              onChange={(e) => {
                setPending(e.target.checked);
                setOffset(0);
              }}
            />{" "}
            Unreviewed only
          </label>
          <div className="queue-list">
            {queue.data?.items.map((e) => (
              <button
                className={`queue-item ${e.epoch_id === selectedId ? "active" : ""}`}
                key={e.epoch_id}
                onClick={() => setSelectedId(e.epoch_id)}
              >
                <span>
                  Epoch {e.epoch_index}
                  <small>
                    {(e.start_s / 60).toFixed(1)} min ·{" "}
                    {e.revision ? "Reviewed" : "Pending"}
                  </small>
                </span>
                <span>
                  <b
                    style={{
                      color: colors[e.prediction?.predicted_stage || ""],
                    }}
                  >
                    {e.prediction?.predicted_stage || "—"}
                  </b>
                  <small>
                    {e.prediction
                      ? e.prediction.review_score.toFixed(3)
                      : "No prediction"}
                  </small>
                </span>
              </button>
            ))}
          </div>
          <div className="pagination">
            <button
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - 20))}
            >
              Previous
            </button>
            <button
              disabled={offset + 20 >= (queue.data?.total || 0)}
              onClick={() => setOffset(offset + 20)}
            >
              Next
            </button>
          </div>
        </aside>
        <div>
          {selected && (
            <>
              <section className="card">
                <div className="section-title">
                  <h2>
                    Epoch {selected.epoch_index}{" "}
                    <span className="muted">
                      / {(selected.start_s / 60).toFixed(1)} min
                    </span>
                  </h2>
                  <div>
                    <button
                      disabled={selected.epoch_index === 0}
                      onClick={() =>
                        setSelectedId(
                          epochs.data![selected.epoch_index - 1].epoch_id,
                        )
                      }
                    >
                      ←
                    </button>
                    <button
                      disabled={
                        selected.epoch_index + 1 === (epochs.data?.length || 0)
                      }
                      onClick={() =>
                        setSelectedId(
                          epochs.data![selected.epoch_index + 1].epoch_id,
                        )
                      }
                    >
                      →
                    </button>
                    <select
                      aria-label="Signal window"
                      value={context}
                      onChange={(e) => setContext(Number(e.target.value))}
                    >
                      <option value={150}>150s context</option>
                      <option value={30}>30s detail</option>
                    </select>
                  </div>
                </div>
                {signal.isLoading ? (
                  <p>Loading signal…</p>
                ) : (
                  signal.data && (
                    <Waveform
                      data={signal.data.channels}
                      start={start}
                      duration={duration}
                      selected={selected}
                    />
                  )
                )}
                <div className="time-label">
                  {start.toFixed(0)} s{" "}
                  <span>
                    Unfiltered native signal · min/max envelope · highlighted
                    selected epoch
                  </span>{" "}
                  {(start + duration).toFixed(0)} s
                </div>
                {spectrum.data && <Spectrogram data={spectrum.data.channels} />}
              </section>
              <section className="card inspector">
                <div>
                  <p className="eyebrow">MODEL EVIDENCE</p>
                  <h2>
                    {selected.prediction?.predicted_stage ||
                      "Prediction unavailable"}{" "}
                    <span className="muted">
                      · priority{" "}
                      {selected.prediction?.review_score.toFixed(3) || "—"}
                    </span>
                  </h2>
                  <p>
                    {selected.prediction?.explanation ||
                      "Incomplete or uncovered epoch; manual review is available."}
                  </p>
                  <div className="probabilities">
                    {Object.entries(
                      selected.prediction?.stage_probabilities || {},
                    ).map(([s, p]) => (
                      <div key={s}>
                        <span>{s}</span>
                        <div className="bar">
                          <i
                            style={{
                              width: `${p * 100}%`,
                              background: colors[s],
                            }}
                          />
                        </div>
                        <b>{(p * 100).toFixed(1)}%</b>
                      </div>
                    ))}
                  </div>
                  <small>
                    Reference:{" "}
                    {selected.reference_stage || "hidden (blinded review)"}
                  </small>
                </div>
                <div>
                  <p className="eyebrow">
                    HUMAN DECISION · REVISION {selected.revision}
                  </p>
                  <label>
                    Reviewed stage
                    <select
                      aria-label="Reviewed stage"
                      value={stage}
                      onChange={(e) => setStage(e.target.value as Stage)}
                    >
                      {stages.map((s) => (
                        <option key={s}>{s}</option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Review note
                    <textarea
                      aria-label="Review note"
                      maxLength={1000}
                      value={note}
                      onChange={(e) => setNote(e.target.value)}
                      placeholder="Reason for correction (optional)"
                    />
                  </label>
                  <div className="actions">
                    <button
                      className="primary"
                      disabled={saving}
                      onClick={save}
                    >
                      {saving ? "Saving…" : "Save revision"}
                    </button>
                    <button
                      onClick={() => {
                        qc.invalidateQueries({ queryKey: ["epochs"] });
                        qc.invalidateQueries({ queryKey: ["history"] });
                      }}
                    >
                      Reload epoch
                    </button>
                  </div>
                  {message && <p role="status">{message}</p>}
                </div>
              </section>
              <section className="card">
                <h2>Revision history</h2>
                <p className="muted">
                  Append-only decisions. To restore a previous decision, select
                  its stage and save a new revision.
                </p>
                {history.data?.items.length === 0 && (
                  <p>No manual revisions yet.</p>
                )}
                {history.data?.items.map((h) => (
                  <div className="history" key={h.revision}>
                    <b>
                      #{h.revision} · {h.stage}
                    </b>
                    <span>{h.note || "No note"}</span>
                    <small>
                      {h.reviewer_id} ·{" "}
                      {new Date(h.created_at).toLocaleString()}
                    </small>
                    <button
                      onClick={() => {
                        setStage(h.stage);
                        setNote(`Restore revision ${h.revision}`);
                      }}
                    >
                      Use stage
                    </button>
                  </div>
                ))}
              </section>
            </>
          )}
        </div>
      </div>
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={client}>
    <App />
  </QueryClientProvider>,
);
