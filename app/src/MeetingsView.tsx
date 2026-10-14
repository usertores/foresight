import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  BASE,
  streamPost,
  type AskSource,
  type AudioDevices,
  type Card,
  type MeetingData,
  type MeetingMeta,
  type RecorderState,
  type ScratchLine,
  type Segment,
} from "./api";
import { HitCard } from "./HitCard";
import { renderAnswer } from "./markdown";
import { isMac, TOGGLE_KEYS } from "./platform";

export function fmtTs(t: number) {
  const s = Math.max(0, Math.floor(t));
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

const SPEAKER: Record<string, string> = { class: "Class", me: "Me" };

/** Class languages offered (Whisper codes); "auto" lets Whisper detect it. */
const LANGUAGES: [string, string][] = [
  ["en", "English"],
  ["es", "Spanish"],
  ["fr", "French"],
  ["de", "German"],
  ["it", "Italian"],
  ["pt", "Portuguese"],
  ["auto", "Detect automatically"],
];

const PREFS_KEY = "foresight.meeting-prefs";
type Prefs = { systemAudio: boolean; micOn: boolean; micId: string; language: string; liveAnswers: boolean };

/** Saved choices, or defaults (the class language comes from the study profile). */
function loadPrefs(language: string): Prefs {
  const defaults: Prefs = { systemAudio: true, micOn: false, micId: "", language, liveAnswers: false };
  try {
    return { ...defaults, ...JSON.parse(localStorage.getItem(PREFS_KEY) ?? "{}") };
  } catch {
    return defaults;
  }
}

function dayLabel(iso: string) {
  const d = new Date(iso);
  const today = new Date();
  const yest = new Date(Date.now() - 86400000);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === yest.toDateString()) return "Yesterday";
  return d.toLocaleDateString("en-US", { weekday: "long", day: "numeric", month: "long" });
}

/** Keep each line's timestamp when it is edited; new lines get `now`. */
function retime(prev: string[], prevT: number[], next: string[], now: number): number[] {
  let a = 0;
  while (a < prev.length && a < next.length && prev[a] === next[a]) a++;
  let b = 0;
  while (
    b < prev.length - a &&
    b < next.length - a &&
    prev[prev.length - 1 - b] === next[next.length - 1 - b]
  )
    b++;
  const oldMid = prev.length - a - b;
  const mid = next.slice(a, next.length - b).map((_, i) => (i < oldMid ? prevT[a + i] ?? now : now));
  return [...prevT.slice(0, a), ...mid, ...prevT.slice(prevT.length - b)];
}

// ============================================================ view

export function MeetingsView(props: {
  active: boolean;
  ready: boolean;
  /** Class language when the user hasn't picked one yet (from the study profile). */
  defaultLanguage: string;
  onRecorder: (r: RecorderState) => void;
}) {
  const { active, ready, defaultLanguage, onRecorder } = props;
  const [list, setList] = useState<MeetingMeta[]>([]);
  const [selected, setSelected] = useState<string>("new");
  const [recorder, setRecorder] = useState<RecorderState>({ state: "idle", id: null, elapsed: 0 });
  const [error, setError] = useState("");
  const [autoNotes, setAutoNotes] = useState<string | null>(null);

  // live state
  const [segments, setSegments] = useState<Segment[]>([]);
  const [interim, setInterim] = useState<Record<string, { t0: number; text: string }>>({});
  const [cards, setCards] = useState<Card[]>([]);
  const [levels, setLevels] = useState<Record<string, number>>({});
  const [startMsg, setStartMsg] = useState("");

  const refreshList = useCallback(() => {
    api
      .meetings()
      .then((r) => {
        setList(r.meetings);
        setRecorder(r.status);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (ready) refreshList();
  }, [ready, refreshList]);

  useEffect(() => onRecorder(recorder), [recorder, onRecorder]);

  // ---------------------------------------------------------- live events (SSE)
  useEffect(() => {
    if (!ready) return;
    const es = new EventSource(`${BASE}/meetings/events`);
    const on = (name: string, fn: (d: any) => void) =>
      es.addEventListener(name, (e) => fn(JSON.parse((e as MessageEvent).data)));
    on("status", (d) => {
      if (d.message) setStartMsg(d.message);
      setRecorder((r) => ({ ...r, state: d.state, id: d.id ?? r.id }));
      if (d.state === "recording") {
        setSegments([]);
        setInterim({});
        setCards([]);
        setSelected(d.id);
        refreshList();
      }
      if (d.state === "idle") refreshList();
    });
    on("level", (d) => {
      setLevels(d.levels);
      setRecorder((r) => (r.state === "recording" ? { ...r, elapsed: d.elapsed } : r));
    });
    on("segment", (d: Segment) => setSegments((s) => [...s, d]));
    on("interim", (d) => setInterim((m) => ({ ...m, [d.source]: d })));
    on("card", (d: Card) => setCards((c) => [d, ...c]));
    on("error", (d) => setError(d.message ?? String(d)));
    return () => es.close();
  }, [ready, refreshList]);

  // When reattaching to a recording already in progress, load what exists so far.
  useEffect(() => {
    if (recorder.state === "recording" && recorder.id && segments.length === 0) {
      api.meeting(recorder.id).then((m) => {
        setSegments(m.segments);
        setCards([...m.cards].reverse());
      });
    }
  }, [recorder.state, recorder.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const start = async (body: Parameters<typeof api.startMeeting>[0]) => {
    setError("");
    setStartMsg(isMac ? "Getting Whisper ready on the PC…" : "Loading Whisper…");
    setRecorder((r) => ({ ...r, state: "starting" }));
    try {
      const meta = await api.startMeeting(body);
      setSelected(meta.id);
      setRecorder({ state: "recording", id: meta.id, elapsed: 0 });
    } catch (e) {
      setError(String((e as Error).message));
      setRecorder((r) => ({ ...r, state: "idle" }));
    }
  };

  const stop = async () => {
    const id = recorder.id;
    // Set before stopping: the "idle" SSE event can arrive before the HTTP reply and
    // mount the detail view, which then writes the notes straight away (like Foresight).
    if (id) setAutoNotes(id);
    setRecorder((r) => ({ ...r, state: "stopping" }));
    try {
      await api.stopMeeting();
      refreshList();
      if (id) setSelected(id);
    } catch (e) {
      setAutoNotes(null);
      setError(String((e as Error).message));
    }
  };

  const groups = useMemo(() => {
    const g: [string, MeetingMeta[]][] = [];
    for (const m of list) {
      const label = dayLabel(m.created);
      const last = g[g.length - 1];
      if (last && last[0] === label) last[1].push(m);
      else g.push([label, [m]]);
    }
    return g;
  }, [list]);

  const recording = recorder.state === "recording" || recorder.state === "stopping";
  const isLive = recording && selected === recorder.id;

  return (
    <div className="view meetings" hidden={!active}>
      <aside className="sidebar">
        <button
          className={`new-btn ${selected === "new" ? "active" : ""}`}
          onClick={() => setSelected(recording && recorder.id ? recorder.id : "new")}
        >
          {recording ? (
            <>
              <span className="rec-dot" /> Recording · {fmtTs(recorder.elapsed)}
            </>
          ) : (
            <>
              <span className="plus">＋</span> New class
            </>
          )}
        </button>
        <nav className="meeting-list">
          {groups.map(([label, items]) => (
            <div key={label}>
              <div className="group-label">{label}</div>
              {items.map((m) => (
                <button
                  key={m.id}
                  className={`meeting-item ${selected === m.id ? "active" : ""}`}
                  onClick={() => setSelected(m.id)}
                >
                  <span className="m-title">
                    {m.status === "recording" && <span className="rec-dot small" />}
                    {m.title}
                  </span>
                  <span className="m-sub">
                    {new Date(m.created).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" })}
                    {m.duration > 0 && ` · ${Math.max(1, Math.round(m.duration / 60))} min`}
                  </span>
                </button>
              ))}
            </div>
          ))}
          {list.length === 0 && <p className="muted pad">You haven't recorded any classes yet.</p>}
        </nav>
      </aside>

      <section className="meeting-main">
        {error && (
          <div className="banner error" onClick={() => setError("")}>
            {error}
          </div>
        )}
        {selected === "new" && !recording ? (
          <Setup
            ready={ready}
            defaultLanguage={defaultLanguage}
            starting={recorder.state === "starting"}
            startMsg={startMsg}
            onStart={start}
          />
        ) : isLive ? (
          <Live
            id={recorder.id!}
            elapsed={recorder.elapsed}
            stopping={recorder.state === "stopping"}
            segments={segments}
            interim={interim}
            cards={cards}
            levels={levels}
            onStop={stop}
          />
        ) : selected !== "new" ? (
          <Detail
            key={selected}
            id={selected}
            autoNotes={autoNotes === selected}
            onAutoNotesDone={() => setAutoNotes(null)}
            onChanged={refreshList}
            onDeleted={() => {
              setSelected("new");
              refreshList();
            }}
          />
        ) : null}
      </section>
    </div>
  );
}

// ============================================================ setup

function Setup(props: {
  ready: boolean;
  defaultLanguage: string;
  starting: boolean;
  startMsg: string;
  onStart: (b: Parameters<typeof api.startMeeting>[0]) => void;
}) {
  const [devices, setDevices] = useState<AudioDevices | null>(null);
  const [prefs, setPrefs] = useState<Prefs>(() => loadPrefs(props.defaultLanguage));
  const [title, setTitle] = useState("");

  useEffect(() => {
    if (props.ready) api.devices().then(setDevices).catch(() => {});
  }, [props.ready]);

  useEffect(() => {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    } catch {}
  }, [prefs]);

  const set = <K extends keyof Prefs>(k: K, v: Prefs[K]) => setPrefs((p) => ({ ...p, [k]: v }));
  const micId = prefs.micId || devices?.mics.find((m) => m.default)?.id || devices?.mics[0]?.id || "";
  const canStart = props.ready && !props.starting && (prefs.systemAudio || (prefs.micOn && micId));

  return (
    <div className="setup">
      <h2>New class</h2>
      <p className="muted">
        Record an online class while you follow it. Whisper transcribes it {isMac ? "on your PC" : "on this computer"}{" "}
        and, when it ends, Gemma 4 turns it into notes.
      </p>

      <label className="field">
        <span>Title (optional)</span>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="e.g. Week 3 — Thermodynamics"
        />
      </label>

      <div className="field">
        <span>What to record</span>
        <label className="check">
          <input
            type="checkbox"
            checked={prefs.systemAudio}
            onChange={(e) => set("systemAudio", e.target.checked)}
          />
          <div>
            Computer audio <span className="muted">(Zoom, Meet, Teams…)</span>
            {devices && <div className="hint">Output: {devices.speaker}</div>}
            {isMac && prefs.systemAudio && (
              <div className="hint">
                The first time, macOS asks for the “Screen & System Audio Recording” permission for Foresight:
                allow it and reopen the app.
              </div>
            )}
          </div>
        </label>
        <label className="check">
          <input type="checkbox" checked={prefs.micOn} onChange={(e) => set("micOn", e.target.checked)} />
          <div>
            My microphone <span className="muted">(in-person classes, or to include your questions)</span>
            {prefs.micOn && devices && (
              <select value={micId} onChange={(e) => set("micId", e.target.value)}>
                {devices.mics.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                  </option>
                ))}
              </select>
            )}
            {prefs.micOn && prefs.systemAudio && (
              <div className="hint">Use headphones so the microphone doesn't pick up the teacher too.</div>
            )}
          </div>
        </label>
      </div>

      <div className="row">
        <label className="field">
          <span>Class language</span>
          <select value={prefs.language} onChange={(e) => set("language", e.target.value)}>
            {LANGUAGES.map(([code, name]) => (
              <option key={code} value={code}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label className="check toggle">
          <input
            type="checkbox"
            checked={prefs.liveAnswers}
            onChange={(e) => set("liveAnswers", e.target.checked)}
          />
          <div>
            Live answers
            <div className="hint">When someone asks a question in class, Gemma answers from your materials.</div>
          </div>
        </label>
      </div>

      <button
        className="primary big"
        disabled={!canStart}
        onClick={() =>
          props.onStart({
            title,
            system_audio: prefs.systemAudio,
            mic_id: prefs.micOn ? micId : null,
            language: prefs.language,
            live_answers: prefs.liveAnswers,
          })
        }
      >
        {props.starting ? (
          <>
            <span className="spinner light" /> {props.startMsg || "Getting ready…"}
          </>
        ) : (
          <>
            <span className="rec-dot static" /> Start recording
          </>
        )}
      </button>
      <p className="hint center">
        You can hide the window (
        {TOGGLE_KEYS.map((k, i) => (
          <span key={k}>
            {i > 0 && "+"}
            <kbd>{k}</kbd>
          </span>
        ))}
        ): recording continues in the background.
      </p>
    </div>
  );
}

// ============================================================ live

function Live(props: {
  id: string;
  elapsed: number;
  stopping: boolean;
  segments: Segment[];
  interim: Record<string, { t0: number; text: string }>;
  cards: Card[];
  levels: Record<string, number>;
  onStop: () => void;
}) {
  const { id, elapsed, stopping, segments, interim, cards, levels, onStop } = props;
  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const multi = Object.keys(levels).length > 1;

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [segments, interim]);

  const interims = Object.entries(interim).filter(([, v]) => v.text);

  return (
    <div className="live">
      <div className="live-bar">
        <span className="rec-dot" />
        <strong>{stopping ? "Finishing the transcription…" : "Recording"}</strong>
        <span className="timer">{fmtTs(elapsed)}</span>
        <div className="meters">
          {Object.entries(levels).map(([src, v]) => (
            <span key={src} className="meter" title={SPEAKER[src]}>
              <span className="meter-label">{SPEAKER[src]}</span>
              <span className="meter-track">
                <span style={{ width: `${Math.round(v * 100)}%` }} />
              </span>
            </span>
          ))}
        </div>
        <span className="spacer" />
        <button className="danger" disabled={stopping} onClick={onStop}>
          {stopping ? <span className="spinner light" /> : <span className="stop-sq" />} Stop and create notes
        </button>
      </div>

      <div className="live-grid">
        <div
          className="transcript card"
          ref={scrollRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
          }}
        >
          {segments.length === 0 && interims.length === 0 && (
            <div className="empty small">
              <div className="pulse" />
              <p>Listening… the transcript appears here as soon as someone speaks.</p>
            </div>
          )}
          {segments.map((s) => (
            <p key={s.id} className="seg">
              <span className="ts">{fmtTs(s.t0)}</span>
              {multi && <span className={`who who-${s.source}`}>{SPEAKER[s.source]}</span>}
              <span>{s.text}</span>
            </p>
          ))}
          {interims.map(([src, v]) => (
            <p key={src} className="seg interim">
              <span className="ts">{fmtTs(v.t0)}</span>
              {multi && <span className={`who who-${src}`}>{SPEAKER[src]}</span>}
              <span>{v.text}…</span>
            </p>
          ))}
        </div>

        <div className="side">
          <Scratch id={id} elapsed={elapsed} />
          <div className="cards-head">From your materials</div>
          <div className="cards">
            {cards.length === 0 && (
              <p className="hint">
                When the teacher talks about something in your materials, it shows up here with the exact page.
              </p>
            )}
            {cards.map((c, i) => (
              <ContextCard key={`${c.t}-${i}`} card={c} />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function Scratch(props: { id: string; elapsed: number }) {
  const [text, setText] = useState("");
  const times = useRef<number[]>([]);
  const elapsedRef = useRef(props.elapsed);
  elapsedRef.current = props.elapsed;
  const saveTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    api.meeting(props.id).then((m) => {
      if (m.scratch.length) {
        setText(m.scratch.map((l) => l.text).join("\n"));
        times.current = m.scratch.map((l) => l.t);
      }
    });
  }, [props.id]);

  const onChange = (next: string) => {
    const prevLines = text.split("\n");
    const nextLines = next.split("\n");
    times.current = retime(prevLines, times.current, nextLines, elapsedRef.current);
    setText(next);
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      const lines: ScratchLine[] = nextLines
        .map((l, i) => ({ t: Math.round(times.current[i] ?? elapsedRef.current), text: l }))
        .filter((l) => l.text.trim());
      api.saveScratch(props.id, lines).catch(() => {});
    }, 800);
  };

  return (
    <div className="scratch">
      <div className="cards-head">My quick notes</div>
      <textarea
        value={text}
        onChange={(e) => onChange(e.target.value)}
        placeholder={"Jot down ideas, one per line:\ncheck the example on p. 131\nexam: this always comes up\nask about the second formula"}
        spellCheck={false}
      />
      <div className="hint">Each line keeps its minute; at the end, Gemma expands them with what was said.</div>
    </div>
  );
}

function ContextCard({ card }: { card: Card }) {
  const open = (h: { path: string; page: number }) => api.open(h.path, h.page).catch(() => {});
  if (card.kind === "answer") {
    return (
      <div className="ctx-card answer-card">
        <div className="ctx-head">
          <span className="badge ai">Answer</span>
          <span className="ts">{fmtTs(card.t)}</span>
        </div>
        <div className="ctx-q">“{card.question}”</div>
        <div
          className="prose small"
          onClick={(e) => {
            const b = (e.target as HTMLElement).closest<HTMLElement>("[data-cite]");
            const h = b && card.hits[Number(b.dataset.cite) - 1];
            if (h) open(h);
          }}
          dangerouslySetInnerHTML={{ __html: renderAnswer(card.answer) }}
        />
      </div>
    );
  }
  return (
    <div className="ctx-card">
      <div className="ctx-head">
        <span className="ts">{fmtTs(card.t)}</span>
      </div>
      {card.hits.map((h, i) => (
        <HitCard key={i} hit={h} compact onOpen={() => open(h)} />
      ))}
    </div>
  );
}

// ============================================================ detail

type Tab = "notes" | "transcript" | "ask" | "context";

function Detail(props: {
  id: string;
  autoNotes: boolean;
  onAutoNotesDone: () => void;
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const { id } = props;
  const [data, setData] = useState<MeetingData | null>(null);
  const [tab, setTab] = useState<Tab>("notes");
  const [title, setTitle] = useState("");
  const [notes, setNotes] = useState("");
  const [genState, setGenState] = useState<"idle" | "running">("idle");
  const [progress, setProgress] = useState("");
  const [genSecs, setGenSecs] = useState(0);
  const [error, setError] = useState("");
  const [exported, setExported] = useState<string>("");
  const [confirmDel, setConfirmDel] = useState(false);
  const [audioSrc, setAudioSrc] = useState("class");
  const [now, setNow] = useState(0);
  const audioRef = useRef<HTMLAudioElement>(null);
  const started = useRef(false);

  const load = useCallback(
    () =>
      api.meeting(id).then((d) => {
        setData(d);
        setTitle(d.meta.title);
        setNotes(d.notes);
        setExported(d.meta.exported_to ?? "");
        if (d.audio.length && !d.audio.includes(audioSrc)) setAudioSrc(d.audio[0]);
        return d;
      }),
    [id], // eslint-disable-line react-hooks/exhaustive-deps
  );

  const generate = useCallback(async () => {
    setGenState("running");
    setError("");
    setNotes("");
    setTab("notes");
    setProgress("Getting ready…");
    const t0 = Date.now();
    setGenSecs(0);
    const tick = window.setInterval(() => setGenSecs(Math.round((Date.now() - t0) / 1000)), 1000);
    try {
      await streamPost(`/meetings/${id}/notes`, {}, (ev, d) => {
        if (ev === "progress") setProgress(d);
        else if (ev === "token") {
          setProgress("Writing the notes…");
          setNotes((n) => n + d);
        } else if (ev === "done") {
          setNotes(d.notes);
          setTitle(d.title);
        }
      });
      await load();
      props.onChanged();
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      window.clearInterval(tick);
      setGenState("idle");
      setProgress("");
    }
  }, [id, load]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    load();
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Fresh recording: once its data is loaded and recording has fully stopped, write notes.
  useEffect(() => {
    if (props.autoNotes && data && data.meta.status === "stopped" && !started.current) {
      started.current = true;
      props.onAutoNotesDone();
      if (data.segments.length && !data.notes) generate();
    } else if (props.autoNotes && data && data.meta.status !== "stopped") {
      const t = window.setTimeout(load, 700); // still draining the last segments
      return () => window.clearTimeout(t);
    }
  }, [props.autoNotes, data]); // eslint-disable-line react-hooks/exhaustive-deps

  const seek = (t: number) => {
    const a = audioRef.current;
    if (!a) return;
    a.currentTime = Math.max(0, t - 1);
    a.play().catch(() => {});
  };

  const saveTitle = () => {
    if (data && title.trim() && title !== data.meta.title) {
      api.renameMeeting(id, title).then(() => props.onChanged());
    }
  };

  const doExport = async () => {
    try {
      const r = await api.exportMeeting(id);
      setExported(r.path);
      await load();
    } catch (e) {
      setError(String((e as Error).message));
    }
  };

  const openExported = () => {
    // The exported note lives inside the indexed folder: open it through the API (Obsidian).
    api.status().then((s) => {
      const root = s.corpus_dir.replace(/\\/g, "/").replace(/\/$/, "");
      const rel = exported.replace(/\\/g, "/").replace(root + "/", "");
      api.open(rel, 0).catch((e) => setError(String(e.message)));
    });
  };

  const notesHtml = useMemo(() => renderAnswer(notes), [notes]);

  if (!data) return <div className="empty"><div className="pulse" /></div>;
  const multi = new Set(data.segments.map((s) => s.source)).size > 1;
  let activeSeg: number | undefined;
  for (const s of data.segments) if (s.t0 <= now + 0.3) activeSeg = s.id;

  return (
    <div className="detail">
      <div className="detail-head">
        <input
          className="title-input"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onBlur={saveTitle}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
        <div className="detail-sub muted">
          {new Date(data.meta.created).toLocaleString("en-US", {
            weekday: "long",
            day: "numeric",
            month: "long",
            hour: "2-digit",
            minute: "2-digit",
          })}
          {" · "}
          {Math.max(1, Math.round(data.meta.duration / 60))} min · {data.segments.length} sentences
        </div>
        <div className="detail-actions">
          {exported && data.meta.export_stale ? (
            <button className="ghost" disabled={genState === "running"} onClick={doExport} title={exported}>
              Update in Obsidian
            </button>
          ) : exported ? (
            <button className="ghost" onClick={openExported} title={exported}>
              ✓ In Obsidian · open
            </button>
          ) : (
            <button className="ghost" disabled={!notes || genState === "running"} onClick={doExport}>
              Save to Obsidian
            </button>
          )}
          <button className="ghost" disabled={genState === "running" || !data.segments.length} onClick={generate}>
            {notes ? "Regenerate notes" : "Create notes"}
          </button>
          {confirmDel ? (
            <button className="danger small" onClick={() => api.deleteMeeting(id).then(props.onDeleted)}>
              Sure? Delete
            </button>
          ) : (
            <button className="ghost" onClick={() => setConfirmDel(true)} onBlur={() => setConfirmDel(false)}>
              Delete
            </button>
          )}
        </div>
      </div>

      {data.audio.length > 0 && (
        <div className="player">
          <audio
            ref={audioRef}
            controls
            preload="metadata"
            src={api.audioUrl(id, audioSrc)}
            onTimeUpdate={(e) => setNow(e.currentTarget.currentTime)}
          />
          {data.audio.length > 1 && (
            <select value={audioSrc} onChange={(e) => setAudioSrc(e.target.value)}>
              {data.audio.map((a) => (
                <option key={a} value={a}>
                  {SPEAKER[a] ?? a}
                </option>
              ))}
            </select>
          )}
        </div>
      )}

      <div className="tabs" role="tablist">
        {(
          [
            ["notes", "Notes"],
            ["transcript", "Transcript"],
            ["ask", "Ask"],
            ["context", `Context (${data.cards.length})`],
          ] as [Tab, string][]
        ).map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}>
            {label}
          </button>
        ))}
      </div>

      {error && <div className="banner error">{error}</div>}

      <div className="tab-body">
        {tab === "notes" &&
          (notes ? (
            <article
              className="prose notes card"
              onClick={(e) => {
                const b = (e.target as HTMLElement).closest<HTMLElement>("[data-cite]");
                const h = b && data.refs[Number(b.dataset.cite) - 1];
                if (h) api.open(h.path, h.page).catch(() => {});
                const ts = (e.target as HTMLElement).closest<HTMLElement>("[data-ts]");
                if (ts) seek(Number(ts.dataset.ts));
              }}
              dangerouslySetInnerHTML={{ __html: linkTimestamps(notesHtml) }}
            />
          ) : genState === "running" ? (
            <div className="card pad">
              <p className="muted">
                <span className="spinner" /> {progress} <span className="tabnum">· {fmtTs(genSecs)}</span>
              </p>
              <div className="skeleton">
                <span />
                <span />
                <span />
              </div>
            </div>
          ) : (
            <div className="empty">
              <p>No notes for this class yet.</p>
              {data.segments.length > 0 && (
                <button className="primary" onClick={generate}>
                  Create notes with Gemma 4
                </button>
              )}
            </div>
          ))}
        {tab === "notes" && notes && genState === "running" && (
          <p className="muted">
            <span className="spinner" /> {progress} <span className="tabnum">· {fmtTs(genSecs)}</span>
          </p>
        )}

        {tab === "transcript" && (
          <div className="transcript card static">
            {data.segments.length === 0 && <p className="muted">No transcript.</p>}
            {data.segments.map((s) => (
              <p key={s.id} className={`seg ${s.id === activeSeg && now > 0 ? "playing" : ""}`}>
                <button className="ts link" onClick={() => seek(s.t0)} title="Listen from here">
                  {fmtTs(s.t0)}
                </button>
                {multi && <span className={`who who-${s.source}`}>{SPEAKER[s.source]}</span>}
                <span>{s.text}</span>
              </p>
            ))}
          </div>
        )}

        {tab === "ask" && <AskClass id={id} onSeek={seek} />}

        {tab === "context" && (
          <div className="cards">
            {data.cards.length === 0 && <p className="muted">This class had no context cards.</p>}
            {[...data.cards].reverse().map((c, i) => (
              <ContextCard key={i} card={c} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** Make [mm:ss] / [h:mm:ss] in rendered notes clickable to play the audio there. */
function linkTimestamps(html: string) {
  return html.replace(/\[(\d{1,2}:)?(\d{1,2}):(\d{2})\]/g, (m, h, mm, ss) => {
    const t = (h ? parseInt(h) * 3600 : 0) + parseInt(mm) * 60 + parseInt(ss);
    return `<button class="tslink" data-ts="${t}">${m.slice(1, -1)}</button>`;
  });
}

function AskClass(props: { id: string; onSeek: (t: number) => void }) {
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState("");
  const [sources, setSources] = useState<AskSource[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const run = async () => {
    if (!q.trim()) return;
    setBusy(true);
    setAnswer("");
    setSources([]);
    setError("");
    try {
      await streamPost(`/meetings/${props.id}/ask`, { question: q }, (ev, d) => {
        if (ev === "sources") setSources(d);
        else if (ev === "token") setAnswer((a) => a + d);
      });
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const openSource = (s: AskSource) => {
    if (s.kind === "class") props.onSeek(s.t0);
    else api.open(s.path, s.page).catch(() => {});
  };

  return (
    <div className="ask-class">
      <div className="searchbar small">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
          placeholder="What did the teacher say about…?"
        />
        {busy && <span className="spinner" />}
        <button className="primary" onClick={run} disabled={busy}>
          Ask
        </button>
      </div>
      {error && <div className="banner error">{error}</div>}
      {answer && (
        <div
          className="prose card pad"
          onClick={(e) => {
            const b = (e.target as HTMLElement).closest<HTMLElement>("[data-cite]");
            const s = b && sources[Number(b.dataset.cite) - 1];
            if (s) openSource(s);
            const ts = (e.target as HTMLElement).closest<HTMLElement>("[data-ts]");
            if (ts) props.onSeek(Number(ts.dataset.ts));
          }}
          dangerouslySetInnerHTML={{ __html: linkTimestamps(renderAnswer(answer)) }}
        />
      )}
      {sources.length > 0 && (
        <div className="results">
          <div className="results-head">Sources</div>
          {sources.map((s, i) =>
            s.kind === "class" ? (
              <article key={i} className="hit card compact" onClick={() => openSource(s)} title="Listen">
                <div className="hit-head">
                  <span className="num">{i + 1}</span>
                  <span className="badge src-class">Class</span>
                  <span className="title">Minute {fmtTs(s.t0)}</span>
                  <span className="page">▶</span>
                </div>
                <p className="text">{s.text.slice(0, 260)}…</p>
              </article>
            ) : (
              <HitCard key={i} hit={s} num={i + 1} compact query={q} onOpen={() => openSource(s)} />
            ),
          )}
        </div>
      )}
    </div>
  );
}
