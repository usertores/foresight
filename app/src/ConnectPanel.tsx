import { useEffect, useState } from "react";
import { api } from "./api";
import { inTauri, pickFolder } from "./platform";

/**
 * "Connect to your PC": a machine that can't run the models (e.g. a Mac) uses the ones on the
 * user's PC (server mode, over Tailscale). Fills the window on first run; a dialog later.
 */
export function ConnectPanel(props: { firstRun: boolean; onClose: () => void }) {
  const { firstRun, onClose } = props;
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [folder, setFolder] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [busy, setBusy] = useState<"" | "test" | "save" | "pick">("");
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    api
      .remoteConfig()
      .then((c) => {
        setUrl(c.url);
        setToken(c.token);
        setFolder(c.corpus_dir);
        if (!c.corpus_exists) setResult({ ok: false, text: "Pick the folder with your study materials." });
      })
      .catch((e) => setResult({ ok: false, text: String(e.message) }));
  }, []);

  useEffect(() => {
    if (firstRun) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [firstRun, onClose]);

  const test = async () => {
    setBusy("test");
    setResult(null);
    try {
      const r = await api.testRemote(url, token);
      setUrl(r.url);
      setResult(
        r.ok
          ? { ok: true, text: `Connected to the PC${r.chat_model ? ` · ${r.chat_model}` : ""}` }
          : { ok: false, text: r.error ?? "Couldn't connect" },
      );
    } catch (e) {
      setResult({ ok: false, text: String((e as Error).message) });
    } finally {
      setBusy("");
    }
  };

  const save = async () => {
    setBusy("save");
    setResult(null);
    try {
      await api.setRemote({ url, token, corpus_dir: folder || undefined });
      onClose();
    } catch (e) {
      setResult({ ok: false, text: String((e as Error).message) });
    } finally {
      setBusy("");
    }
  };

  const pick = async () => {
    setBusy("pick");
    try {
      const picked = await pickFolder(folder);
      if (picked) setFolder(picked);
    } catch (e) {
      setResult({ ok: false, text: String((e as Error).message ?? e) });
    } finally {
      setBusy("");
    }
  };

  const panel = (
    <div className="modal connect" role="dialog" aria-label="Connect to your PC">
      <div className="modal-head">
        <h2>Connect to your PC</h2>
        {!firstRun && (
          <button className="ghost" onClick={onClose} aria-label="Close">
            ✕
          </button>
        )}
      </div>
      <p className="muted">
        This computer uses your PC's models (search, Gemma 4 and Whisper) over Tailscale. Your files stay
        here. On the PC: status bar → <strong>Server</strong> → turn on{" "}
        <em>Serve models to my other computers</em> and copy the address and the password.
      </p>

      <label className="field">
        <span>PC address</span>
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="http://my-pc:8770"
          spellCheck={false}
          autoFocus
        />
      </label>

      <div className="field">
        <span>Password</span>
        <div className="input-row">
          <input
            type={showToken ? "text" : "password"}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            spellCheck={false}
          />
          <button className="ghost" onClick={() => setShowToken((v) => !v)}>
            {showToken ? "Hide" : "Show"}
          </button>
        </div>
      </div>

      <div className="field">
        <span>Study folder</span>
        <div className="input-row">
          <input value={folder} onChange={(e) => setFolder(e.target.value)} spellCheck={false} />
          {inTauri && (
            <button className="ghost" disabled={!!busy} onClick={pick}>
              Browse…
            </button>
          )}
        </div>
        <div className="hint">
          Indexed on this computer (PDFs and Markdown notes). The first time takes a few minutes; the PC does the
          heavy lifting.
        </div>
      </div>

      {result && <div className={`banner ${result.ok ? "ok" : "error"}`}>{result.text}</div>}

      <div className="modal-actions">
        <button className="ghost" disabled={!!busy || !url || !token} onClick={test}>
          {busy === "test" ? <span className="spinner" /> : null} Test connection
        </button>
        <button className="primary" disabled={!!busy || !url || !token || !folder} onClick={save}>
          {busy === "save" ? <span className="spinner light" /> : null} Connect
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
