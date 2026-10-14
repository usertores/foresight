import { useEffect, useState } from "react";
import { api } from "./api";
import { inTauri, pickFolder } from "./platform";

/**
 * "Choose your study folder": which folder gets indexed. Fills the window on first run;
 * a dialog to change it later. (Remote machines pick it in ConnectPanel.)
 */
export function FolderPanel(props: { firstRun: boolean; current: string; onClose: () => void }) {
  const { firstRun, onClose } = props;
  const [folder, setFolder] = useState(firstRun ? "" : props.current);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (firstRun) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [firstRun, onClose]);

  const browse = async () => {
    const picked = await pickFolder(folder).catch((e) => {
      setError(String(e.message ?? e));
      return null;
    });
    if (picked) setFolder(picked);
  };

  const save = async () => {
    setBusy(true);
    setError("");
    try {
      await api.settings({ corpus_dir: folder.trim() });
      onClose();
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const panel = (
    <div className="modal connect" role="dialog" aria-label="Study folder">
      <div className="modal-head">
        <h2>{firstRun ? "Welcome to Foresight" : "Study folder"}</h2>
        {!firstRun && (
          <button className="ghost" onClick={onClose} aria-label="Close">
            ✕
          </button>
        )}
      </div>
      <p className="muted">
        Pick the folder with your study materials: PDFs and Markdown notes (an Obsidian vault works too),
        subfolders included. Each top-level subfolder becomes a search filter, so a layout like{" "}
        <em>Books</em>, <em>Slides</em>, <em>Exams</em> gives you those filters. Nothing leaves this computer.
      </p>

      <div className="field">
        <span>Folder</span>
        <div className="input-row">
          <input
            value={folder}
            onChange={(e) => setFolder(e.target.value)}
            placeholder={inTauri ? "Choose a folder…" : "Path to the folder"}
            spellCheck={false}
            autoFocus
          />
          {inTauri && (
            <button className="ghost" disabled={busy} onClick={browse}>
              Browse…
            </button>
          )}
        </div>
        <div className="hint">
          The first index takes a while (about a minute per 1,000 pages on a laptop GPU); after that only new
          or changed files are processed.
          {!firstRun && " Changing the folder rebuilds the index."}
        </div>
      </div>

      {error && <div className="banner error">{error}</div>}

      <div className="modal-actions">
        {!firstRun && (
          <button className="ghost" disabled={busy} onClick={onClose}>
            Cancel
          </button>
        )}
        <button className="primary" disabled={busy || !folder.trim()} onClick={save}>
          {busy ? <span className="spinner light" /> : null} {firstRun ? "Start indexing" : "Use this folder"}
        </button>
      </div>
    </div>
  );

  return firstRun ? (
    <div className="connect-page">{panel}</div>
  ) : (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      {panel}
    </div>
  );
}
