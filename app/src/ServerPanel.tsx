import { useCallback, useEffect, useState } from "react";
import { api, type GatewayStatus } from "./api";

function ago(ts: number) {
  if (!ts) return "never";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

/** "Use this PC as a server": lend the models to the user's other computers. */
export function ServerPanel({ onClose }: { onClose: () => void }) {
  const [st, setSt] = useState<GatewayStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState("");
  const [showToken, setShowToken] = useState(false);

  const refresh = useCallback(() => api.gateway().then(setSt).catch((e) => setError(String(e.message))), []);

  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 4000);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => {
      window.clearInterval(t);
      window.removeEventListener("keydown", onKey);
    };
  }, [refresh, onClose]);

  const set = async (body: { enabled?: boolean; regenerate_token?: boolean }) => {
    setBusy(true);
    setError("");
    try {
      setSt(await api.setGateway(body));
    } catch (e) {
      setError(String((e as Error).message));
      refresh();
    } finally {
      setBusy(false);
    }
  };

  const copy = (label: string, text: string) => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(label);
      window.setTimeout(() => setCopied(""), 1500);
    });
  };

  const ts = st?.tailscale;
  const tsReady = !!ts?.installed && !!ts?.running && !!ts?.ip;
  // Prefer the Tailscale name; fall back to the IP, or the bound address if not on Tailscale.
  const url = st ? st.url_name || st.url_ip || (st.address ? `http://${st.address}` : "") : "";

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label="Use this PC as a server">
        <div className="modal-head">
          <h2>Use this PC as a server</h2>
          <button className="ghost" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <p className="muted">
          Your other computers (e.g. a Mac) can use this PC's models (search, Gemma 4 and Whisper) over
          Tailscale. Their files stay on them: this PC only processes text passages and class audio.
        </p>

        {!st ? (
          <div className="pulse" />
        ) : (
          <>
            <div className={`ts-state ${tsReady ? "ok" : "warn"}`}>
              <span className={`dot ${tsReady ? "ok" : "warn"}`} />
              {!ts?.installed ? (
                <span>Tailscale isn't installed on this PC.</span>
              ) : !tsReady ? (
                <span>Tailscale is installed but not connected: open it from the tray and sign in.</span>
              ) : (
                <span>
                  Tailscale connected as <strong>{ts?.short_name}</strong> ({ts?.ip})
                </span>
              )}
            </div>

            <label className="check toggle big-toggle">
              <input
                type="checkbox"
                checked={st.enabled}
                disabled={busy || (!st.enabled && !tsReady)}
                onChange={(e) => set({ enabled: e.target.checked })}
              />
              <div>
                Serve models to my other computers
                <div className="hint">
                  {st.running
                    ? `On at ${st.address}. While it is on, the PC doesn't sleep when idle (closing the lid still does).`
                    : st.enabled
                      ? "Turned on, but not running (see the message)."
                      : "Off: the app works on this PC only."}
                </div>
              </div>
            </label>

            {(error || st.error) && <div className="banner error">{error || st.error}</div>}

            {st.running && (
              <div className="conn">
                <div className="conn-row">
                  <span className="conn-label">Address</span>
                  <code>{url}</code>
                  <button className="ghost" onClick={() => copy("url", url)}>
                    {copied === "url" ? "Copied" : "Copy"}
                  </button>
                </div>
                {st.url_name && st.url_ip && (
                  <div className="conn-row">
                    <span className="conn-label">or by IP</span>
                    <code>{st.url_ip}</code>
                    <button className="ghost" onClick={() => copy("ip", st.url_ip)}>
                      {copied === "ip" ? "Copied" : "Copy"}
                    </button>
                  </div>
                )}
                <div className="conn-row">
                  <span className="conn-label">Password</span>
                  <code>{showToken ? st.token : "•".repeat(16)}</code>
                  <button className="ghost" onClick={() => setShowToken((v) => !v)}>
                    {showToken ? "Hide" : "Show"}
                  </button>
                  <button className="ghost" onClick={() => copy("token", st.token)}>
                    {copied === "token" ? "Copied" : "Copy"}
                  </button>
                </div>
                <div className="hint">
                  Last used from another computer: {ago(st.last_seen)}
                  {st.remote_class && " · recording a class from another computer"}
                </div>
                <button
                  className="link danger-link"
                  disabled={busy}
                  onClick={() => set({ regenerate_token: true })}
                  title="The old password stops working immediately"
                >
                  Change the password
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
