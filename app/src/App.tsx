import { useCallback, useEffect, useState } from "react";
import { api, type RecorderState, type Status } from "./api";
import { ConnectPanel } from "./ConnectPanel";
import { FolderPanel } from "./FolderPanel";
import { fmtTs, MeetingsView } from "./MeetingsView";
import { inTauri, MOD, modKey } from "./platform";
import { fmt, SearchView } from "./SearchView";
import { ServerPanel } from "./ServerPanel";
import "./App.css";
import "./meetings.css";

type View = "search" | "meetings";

const MODEL_LABELS: Record<string, string> = {
  "gemma-4-e2b-it-qat.gguf": "Gemma 4 E2B · fast",
  "gemma-4-e4b-it-qat.gguf": "Gemma 4 E4B · quality",
};

/** "C:\Users\me\Study\Biology" -> "Biology" */
const folderName = (path: string) => path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [offline, setOffline] = useState(false);
  const [view, setView] = useState<View>("search");
  const [focusTick, setFocusTick] = useState(0);
  const [recorder, setRecorder] = useState<RecorderState>({ state: "idle", id: null, elapsed: 0 });
  const [error, setError] = useState("");
  const [serverOpen, setServerOpen] = useState(false);
  const [connectOpen, setConnectOpen] = useState(false);
  const [folderOpen, setFolderOpen] = useState(false);

  // ---------------------------------------------------------------- status polling
  const refresh = useCallback(() => {
    api.status().then(setStatus).catch(() => {});
  }, []);

  useEffect(() => {
    let alive = true;
    let timer: number;
    const tick = async () => {
      try {
        const s = await api.status();
        if (!alive) return;
        setStatus(s);
        setOffline(false);
        const busyIndex = ["scanning", "indexing", "finalizing"].includes(s.index.state);
        timer = window.setTimeout(tick, s.boot.state !== "ready" || busyIndex ? 1000 : 5000);
      } catch {
        if (!alive) return;
        setOffline(true);
        timer = window.setTimeout(tick, 1000);
      }
    };
    tick();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, []);

  // ---------------------------------------------------------------- Tauri hooks
  useEffect(() => {
    if (!inTauri) return;
    let unlisten: (() => void) | undefined;
    import("@tauri-apps/api/event").then(({ listen }) =>
      listen("focus-search", () => setFocusTick((t) => t + 1)).then((u) => (unlisten = u)),
    );
    return () => unlisten?.();
  }, []);

  const hideWindow = useCallback(async () => {
    if (!inTauri) return;
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    getCurrentWindow().hide();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (modKey(e) && (e.key === "1" || e.key === "2")) {
        e.preventDefault();
        setView(e.key === "1" ? "search" : "meetings");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const ready = status?.boot.state === "ready";
  const idx = status?.index;
  const indexing = idx && ["scanning", "indexing", "finalizing"].includes(idx.state);
  const recording = recorder.state === "recording" || recorder.state === "stopping";

  // A remote machine that doesn't know where the PC is yet: nothing works without it.
  if (status?.boot.state === "setup" && !offline) {
    return <ConnectPanel firstRun onClose={() => {}} />;
  }
  // First run on a machine with the models: ask which folder to index.
  if (status && !status.remote && !status.corpus_exists && !offline) {
    return <FolderPanel firstRun current={status.corpus_dir} onClose={refresh} />;
  }

  return (
    <div className="shell">
      <nav className="nav" role="tablist">
        <button role="tab" aria-selected={view === "search"} onClick={() => setView("search")} title={`${MOD}+1`}>
          Search
        </button>
        <button role="tab" aria-selected={view === "meetings"} onClick={() => setView("meetings")} title={`${MOD}+2`}>
          Classes
          {recording && (
            <span className="nav-rec">
              <span className="rec-dot small" /> {fmtTs(recorder.elapsed)}
            </span>
          )}
        </button>
      </nav>

      <div className="views">
        <SearchView
          status={status}
          offline={offline}
          active={view === "search"}
          focusTick={focusTick}
          onHide={hideWindow}
          onConnect={() => setConnectOpen(true)}
        />
        <MeetingsView
          active={view === "meetings"}
          ready={!!ready}
          defaultLanguage={status?.profile.language ?? "en"}
          onRecorder={setRecorder}
        />
      </div>

      <footer className="status">
        <span className={`dot ${offline ? "off" : !ready ? "warn" : indexing ? "busy" : "ok"}`} />
        {offline ? (
          <span>Engine stopped</span>
        ) : indexing && idx ? (
          <span className="indexing">
            Indexing {fmt(idx.files_done)}/{fmt(idx.files_total)}
            <span className="bar">
              <span style={{ width: `${(100 * idx.files_done) / Math.max(idx.files_total, 1)}%` }} />
            </span>
            <span className="muted ellipsis">{idx.current}</span>
          </span>
        ) : (
          <span>
            {fmt(idx?.rows ?? 0)} passages · {fmt(idx?.files ?? 0)} files
            {status?.remote
              ? " · models on the PC"
              : status?.runtime.device && status.runtime.device !== "none"
                ? " · GPU"
                : " · CPU"}
            {idx && idx.needs_ocr.length > 0 && (
              <span className="muted" title={idx.needs_ocr.join("\n")}>
                {" "}
                · {idx.needs_ocr.length} scanned PDFs without OCR
              </span>
            )}
          </span>
        )}
        {recording && view !== "meetings" && (
          <button className="link rec-link" onClick={() => setView("meetings")}>
            <span className="rec-dot small" /> Recording class · {fmtTs(recorder.elapsed)}
          </button>
        )}
        {error && <span className="err-inline">{error}</span>}
        <span className="spacer" />
        {status?.remote && (
          <button
            className="link server-link"
            onClick={() => setConnectOpen(true)}
            title={status.runtime.embed ? `Models on ${status.runtime.remote}` : status.runtime.error}
          >
            <span className={`dot ${status.runtime.embed ? "ok" : "off"}`} />
            {status.runtime.embed ? "PC connected" : "PC disconnected"}
          </button>
        )}
        {status?.gateway && (
          <button
            className="link server-link"
            onClick={() => setServerOpen(true)}
            title="Use this PC as a server for your other computers"
          >
            <span className={`dot ${status.gateway.running ? (status.gateway.remote_class ? "busy" : "ok") : "off"}`} />
            Server
            {status.gateway.running ? (status.gateway.remote_class ? " · remote class" : " · on") : ""}
          </button>
        )}
        {status && status.chat_models.length > 1 && (
          <select
            value={status.runtime.chat_selected}
            onChange={(e) => api.settings({ chat_model: e.target.value }).catch((err) => setError(String(err.message)))}
            title="Model that answers and writes notes. While a class is recorded E2B is always used, to keep up."
          >
            {status.chat_models.map((m) => (
              <option key={m} value={m}>
                {MODEL_LABELS[m] ?? m}
              </option>
            ))}
          </select>
        )}
        {status && !status.remote && (
          <button
            className="link"
            disabled={!!indexing}
            onClick={() => setFolderOpen(true)}
            title={`${status.corpus_dir}\nClick to change the folder`}
          >
            {folderName(status.corpus_dir)}
          </button>
        )}
        <button
          className="link"
          disabled={!ready || !!indexing}
          onClick={() => api.reindex().catch((e) => setError(String(e.message ?? e)))}
          title={status?.corpus_dir}
        >
          Reindex
        </button>
      </footer>
      {serverOpen && <ServerPanel onClose={() => setServerOpen(false)} />}
      {connectOpen && <ConnectPanel firstRun={false} onClose={() => setConnectOpen(false)} />}
      {folderOpen && status && (
        <FolderPanel
          firstRun={false}
          current={status.corpus_dir}
          onClose={() => {
            setFolderOpen(false);
            refresh();
          }}
        />
      )}
    </div>
  );
}
