import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type Hit, type Status } from "./api";
import { HitCard } from "./HitCard";
import { renderAnswer } from "./markdown";
import { ALT, isMac, MOD, modKey } from "./platform";

type Mode = "search" | "ask";

export function fmt(n: number) {
  return n.toLocaleString("en-US");
}

export function SearchView(props: {
  status: Status | null;
  offline: boolean;
  active: boolean;
  focusTick: number;
  onHide: () => void;
  /** Remote machine: open "Connect to your PC". */
  onConnect: () => void;
}) {
  const { status, offline, active, focusTick, onHide, onConnect } = props;
  const [mode, setMode] = useState<Mode>("search");
  const [query, setQuery] = useState("");
  const [ran, setRan] = useState<{ q: string; mode: Mode } | null>(null);
  const [filters, setFilters] = useState<string[]>([]);
  const [hits, setHits] = useState<Hit[]>([]);
  const [ms, setMs] = useState<number | null>(null);
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sel, setSel] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (active) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [active, focusTick]);

  const run = useCallback(
    async (m: Mode = mode) => {
      const q = query.trim();
      if (!q) return;
      abortRef.current?.abort();
      setError("");
      setSel(0);
      setRan({ q, mode: m });
      setBusy(true);
      if (m === "search") {
        setAnswer("");
        try {
          const r = await api.search(q, filters);
          setHits(r.hits);
          setMs(r.ms);
        } catch (e) {
          setError(String((e as Error).message ?? e));
        } finally {
          setBusy(false);
        }
        return;
      }
      setHits([]);
      setAnswer("");
      setMs(null);
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      try {
        await api.ask(
          q,
          filters,
          { onSources: (h) => setHits(h), onToken: (t) => setAnswer((a) => a + t) },
          ctrl.signal,
        );
      } catch (e) {
        if ((e as Error).name !== "AbortError") setError(String((e as Error).message ?? e));
      } finally {
        if (abortRef.current === ctrl) setBusy(false);
      }
    },
    [mode, query, filters],
  );

  const open = useCallback((h: { path: string; page: number }) => {
    api
      .open(h.path, h.page)
      .then((r) => r.note && setError(r.note))
      .catch((e) => setError(String(e.message ?? e)));
  }, []);

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (modKey(e)) {
        setMode("ask");
        run("ask");
      } else if (e.altKey && hits[sel]) {
        open(hits[sel]);
      } else {
        run();
      }
    } else if (e.key === "Tab" && !e.shiftKey) {
      e.preventDefault();
      setMode((m) => (m === "search" ? "ask" : "search"));
    } else if (e.key === "ArrowDown" && hits.length) {
      e.preventDefault();
      setSel((i) => Math.min(i + 1, hits.length - 1));
    } else if (e.key === "ArrowUp" && hits.length) {
      e.preventDefault();
      setSel((i) => Math.max(i - 1, 0));
    } else if (e.key === "Escape") {
      if (busy) abortRef.current?.abort();
      else if (query) setQuery("");
      else onHide();
    }
  };

  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(`[data-idx="${sel}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  const toggleFilter = (s: string) =>
    setFilters((f) => (f.includes(s) ? f.filter((x) => x !== s) : [...f, s]));

  // Biggest collections first.
  const sources = useMemo(
    () => Object.entries(status?.sources ?? {}).sort(([a, n], [b, m]) => m - n || a.localeCompare(b)),
    [status?.sources],
  );
  const subject = status?.profile.subject;
  const examples = status?.profile.examples ?? [];

  const answerHtml = useMemo(() => renderAnswer(answer), [answer]);

  const onAnswerClick = (e: React.MouseEvent) => {
    const btn = (e.target as HTMLElement).closest<HTMLElement>("[data-cite]");
    if (!btn) return;
    const h = hits[Number(btn.dataset.cite) - 1];
    if (h) open(h);
  };

  const ready = status?.boot.state === "ready";

  return (
    <div className="view" hidden={!active}>
      <header className="top">
        <div className="searchbar">
          <svg className="icon" viewBox="0 0 24 24" aria-hidden>
            {mode === "search" ? (
              <path d="M10.5 3a7.5 7.5 0 0 1 5.92 12.1l4.24 4.24-1.42 1.42-4.24-4.24A7.5 7.5 0 1 1 10.5 3Zm0 2a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11Z" />
            ) : (
              <path d="M12 2a1 1 0 0 1 .95.68l1.6 4.77 4.77 1.6a1 1 0 0 1 0 1.9l-4.77 1.6-1.6 4.77a1 1 0 0 1-1.9 0l-1.6-4.77-4.77-1.6a1 1 0 0 1 0-1.9l4.77-1.6 1.6-4.77A1 1 0 0 1 12 2Zm6 13a1 1 0 0 1 .95.68l.43 1.3 1.3.43a1 1 0 0 1 0 1.9l-1.3.43-.43 1.3a1 1 0 0 1-1.9 0l-.43-1.3-1.3-.43a1 1 0 0 1 0-1.9l1.3-.43.43-1.3A1 1 0 0 1 18 15Z" />
            )}
          </svg>
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={
              mode === "search"
                ? `Search for a concept in your ${subject ? subject + " " : ""}materials…`
                : "Ask, and Gemma 4 answers from your materials…"
            }
            spellCheck={false}
          />
          {busy && <span className="spinner" aria-label="Loading" />}
          <div className="segmented" role="tablist">
            <button
              role="tab"
              aria-selected={mode === "search"}
              onClick={() => setMode("search")}
              title="Semantic search (Enter)"
            >
              Search
            </button>
            <button
              role="tab"
              aria-selected={mode === "ask"}
              onClick={() => setMode("ask")}
              title={`Answer with Gemma 4 (${MOD}+Enter)`}
            >
              Ask
            </button>
          </div>
        </div>

        {sources.length > 0 && (
          <div className="chips">
            <button className="chip" aria-pressed={filters.length === 0} onClick={() => setFilters([])}>
              All
            </button>
            {sources.map(([s, n]) => (
              <button
                key={s}
                className="chip"
                aria-pressed={filters.includes(s)}
                onClick={() => toggleFilter(s)}
              >
                {s} <span className="count">{fmt(n)}</span>
              </button>
            ))}
          </div>
        )}
      </header>

      <main className="content" ref={listRef}>
        {error && <div className="banner error">{error}</div>}

        {!ran && (
          <section className="empty">
            {!offline && !ready && status?.remote ? (
              <>
                <div className="pulse" />
                <h2>{status.boot.state === "waiting" ? "Waiting for the PC…" : "Connecting to the PC…"}</h2>
                <p>Gemma 4, search and Whisper run on your PC; your files stay on this computer.</p>
                {["waiting", "error"].includes(status.boot.state) && (
                  <p className="banner error">{status.boot.error}</p>
                )}
                <p>
                  <button className="link" onClick={onConnect}>
                    Change connection
                  </button>
                </p>
              </>
            ) : offline || !ready ? (
              <>
                <div className="pulse" />
                <h2>{offline ? "Starting the local engine…" : "Loading the models…"}</h2>
                <p>
                  {isMac
                    ? "The models run on your PC; your files stay on this Mac."
                    : "EmbeddingGemma 2 and Gemma 4 run on this computer. It takes a few seconds."}
                </p>
                {status?.boot.state === "error" && <p className="banner error">{status.boot.error}</p>}
              </>
            ) : (
              <>
                <h2>Your {subject ? `${subject} materials` : "notes"}, searchable by meaning</h2>
                <p>
                  Type in any language. <kbd>Enter</kbd> searches, <kbd>{MOD}</kbd>+<kbd>Enter</kbd>{" "}
                  asks Gemma 4, <kbd>Tab</kbd> switches mode, <kbd>{ALT}</kbd>+<kbd>Enter</kbd> opens the
                  selected result.
                </p>
                <div className="examples">
                  {examples.map((ex) => (
                    <button
                      key={ex}
                      onClick={() => {
                        setQuery(ex);
                        inputRef.current?.focus();
                      }}
                    >
                      {ex}
                    </button>
                  ))}
                </div>
              </>
            )}
          </section>
        )}

        {ran?.mode === "ask" && (
          <section className="answer card">
            <div className="answer-head">
              <span className="badge ai">Gemma 4</span>
              <span className="muted">{ran.q}</span>
            </div>
            {answer ? (
              <div className="prose" onClick={onAnswerClick} dangerouslySetInnerHTML={{ __html: answerHtml }} />
            ) : (
              <div className="skeleton">
                <span />
                <span />
                <span />
              </div>
            )}
          </section>
        )}

        {ran && hits.length > 0 && (
          <section className="results">
            <div className="results-head">
              {ran.mode === "ask" ? "Sources used" : `${hits.length} results`}
              {ms !== null && <span className="muted"> · {ms} ms</span>}
            </div>
            {hits.map((h, i) => (
              <HitCard
                key={h.id}
                hit={h}
                idx={i}
                num={ran.mode === "ask" ? i + 1 : undefined}
                query={ran.q}
                selected={i === sel}
                onOpen={() => {
                  setSel(i);
                  open(h);
                }}
                onHover={() => setSel(i)}
              />
            ))}
          </section>
        )}

        {ran && !busy && hits.length === 0 && !error && (
          <section className="empty">
            <p>No results. Try other words or remove filters.</p>
          </section>
        )}
      </main>
    </div>
  );
}
