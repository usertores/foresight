// VITE_API_BASE lets a dev build talk to a test backend on another port.
export const BASE: string = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8765/api";

export type Hit = {
  id: string;
  path: string;
  title: string;
  source: string;
  section: string;
  page: number;
  text: string;
  score: number;
  similarity: number | null;
  keyword: boolean;
};

export type IndexProgress = {
  state: "idle" | "scanning" | "indexing" | "finalizing" | "done" | "error";
  files_total: number;
  files_done: number;
  chunks_done: number;
  current: string;
  skipped_duplicates: number;
  errors: string[];
  needs_ocr: string[];
  rows: number;
  files: number;
};

export type Status = {
  /** setup: a remote machine not yet told where the PC is; waiting: the PC doesn't answer (error says why). */
  boot: { state: "starting" | "setup" | "waiting" | "ready" | "error"; error: string };
  runtime: {
    device: string;
    embed: boolean;
    chat: boolean;
    chat_model: string;
    chat_selected: string;
    /** Remote only: the PC's address and why it can't be reached. */
    remote?: string;
    error?: string;
  };
  index: IndexProgress;
  sources: Record<string, number>;
  corpus_dir: string;
  /** False until the user picks the folder to index (first run). */
  corpus_exists: boolean;
  /** The study profile: subject name, default class language, example questions. */
  profile: { subject: string; language: string; examples: string[] };
  chat_models: string[];
  /** True on a machine that uses another PC's models (e.g. a Mac). */
  remote: boolean;
  /** Server mode summary; null on a remote machine. */
  gateway: { enabled: boolean; running: boolean; remote_class: boolean; last_seen: number } | null;
};

// ------------------------------------------------------------ server mode

export type GatewayStatus = {
  enabled: boolean;
  running: boolean;
  error: string;
  address: string;
  url_name: string;
  url_ip: string;
  token: string;
  tailscale: {
    installed: boolean;
    running: boolean;
    state?: string;
    ip?: string;
    name?: string;
    short_name?: string;
    error?: string;
  };
  last_seen: number;
  requests: number;
  remote_class: boolean;
};

/** Remote machine (e.g. a Mac): which PC lends the models, and the folder indexed here. */
export type RemoteConfig = {
  url: string;
  token: string;
  corpus_dir: string;
  corpus_exists: boolean;
  default_port: number;
};

export type RemoteTest = { ok: boolean; url: string; error?: string; chat_model?: string; device?: string };

// ------------------------------------------------------------ meetings

export type MeetingMeta = {
  id: string;
  title: string;
  created: string;
  duration: number;
  language: string;
  sources: string[];
  status: "recording" | "stopped";
  live_answers: boolean;
  exported_to?: string;
  export_stale?: boolean;
};

export type Segment = { id: number; source: string; t0: number; t1: number; text: string };

export type SlimHit = Pick<Hit, "path" | "title" | "source" | "section" | "page" | "text" | "similarity">;

export type Card = {
  t: number;
  kind: "context" | "answer";
  hits: SlimHit[];
  question: string;
  answer: string;
};

export type ScratchLine = { t: number; text: string };

export type MeetingData = {
  meta: MeetingMeta;
  segments: Segment[];
  scratch: ScratchLine[];
  cards: Card[];
  notes: string;
  refs: Hit[];
  audio: string[];
};

export type RecorderState = {
  state: "idle" | "starting" | "recording" | "stopping";
  id: string | null;
  elapsed: number;
};

export type AudioDevices = {
  speaker: string;
  mics: { id: string; name: string; default: boolean }[];
};

/** A class-transcript window or a library hit, as returned by meeting Q&A. */
export type AskSource =
  | { kind: "class"; t0: number; text: string; title: string }
  | ({ kind: "material" } & Hit);

// ------------------------------------------------------------ helpers

async function json<T>(r: Response): Promise<T> {
  if (!r.ok) {
    let msg = r.statusText;
    try {
      msg = (await r.json()).detail ?? msg;
    } catch {}
    throw new Error(msg);
  }
  return r.json();
}

const send = (method: string, path: string, body?: unknown) =>
  fetch(`${BASE}${path}`, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

/** POST that answers with text/event-stream; calls onEvent for each (event, data). */
export async function streamPost(
  path: string,
  body: unknown,
  onEvent: (event: string, data: any) => void,
  signal?: AbortSignal,
) {
  const r = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
    signal,
  });
  if (!r.ok || !r.body) await json(r);
  const reader = r.body!.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let sep: number;
    while ((sep = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      const event = /^event: (.*)$/m.exec(raw)?.[1] ?? "message";
      const data = JSON.parse(/^data: (.*)$/m.exec(raw)?.[1] ?? "null");
      if (event === "error") throw new Error(typeof data === "string" ? data : JSON.stringify(data));
      onEvent(event, data);
    }
  }
}

export const api = {
  status: () => fetch(`${BASE}/status`).then((r) => json<Status>(r)),

  search: (q: string, sources: string[], k = 12) => {
    const p = new URLSearchParams({ q, k: String(k) });
    sources.forEach((s) => p.append("source", s));
    return fetch(`${BASE}/search?${p}`).then((r) => json<{ hits: Hit[]; ms: number }>(r));
  },

  /** note: something the user should know, e.g. a permission Preview needs on macOS. */
  open: (path: string, page: number) =>
    send("POST", "/open", { path, page }).then((r) => json<{ ok: boolean; note?: string }>(r)),

  reindex: (force = false) => send("POST", "/index", { force }).then((r) => json(r)),

  settings: (body: { corpus_dir?: string; chat_model?: string }) =>
    send("POST", "/settings", body).then((r) => json(r)),

  /** Streams an answer. Calls onSources once, then onToken per text delta. */
  ask: (
    question: string,
    sources: string[],
    handlers: { onSources: (h: Hit[]) => void; onToken: (t: string) => void },
    signal: AbortSignal,
  ) =>
    streamPost(
      "/ask",
      { question, sources: sources.length ? sources : null },
      (event, data) => {
        if (event === "sources") handlers.onSources(data);
        else if (event === "token") handlers.onToken(data);
      },
      signal,
    ),

  // ---------------------------------------------------------- connection to the PC (remote)
  remoteConfig: () => fetch(`${BASE}/remote`).then((r) => json<RemoteConfig>(r)),
  testRemote: (url: string, token: string) =>
    send("POST", "/remote/test", { url, token }).then((r) => json<RemoteTest>(r)),
  setRemote: (body: { url: string; token: string; corpus_dir?: string }) =>
    send("POST", "/remote", body).then((r) => json<{ ok: boolean; url: string }>(r)),

  // ---------------------------------------------------------- server mode
  gateway: () => fetch(`${BASE}/gateway`).then((r) => json<GatewayStatus>(r)),
  setGateway: (body: { enabled?: boolean; regenerate_token?: boolean }) =>
    send("POST", "/gateway", body).then((r) => json<GatewayStatus>(r)),

  // ---------------------------------------------------------- meetings

  devices: () => fetch(`${BASE}/audio/devices`).then((r) => json<AudioDevices>(r)),
  meetings: () =>
    fetch(`${BASE}/meetings`).then((r) =>
      json<{ meetings: MeetingMeta[]; status: RecorderState }>(r),
    ),
  meeting: (id: string) => fetch(`${BASE}/meetings/${id}`).then((r) => json<MeetingData>(r)),
  startMeeting: (body: {
    title: string;
    system_audio: boolean;
    mic_id: string | null;
    language: string;
    live_answers: boolean;
  }) => send("POST", "/meetings/start", body).then((r) => json<MeetingMeta>(r)),
  stopMeeting: () => send("POST", "/meetings/stop").then((r) => json<MeetingMeta>(r)),
  renameMeeting: (id: string, title: string) =>
    send("PATCH", `/meetings/${id}`, { title }).then((r) => json<MeetingMeta>(r)),
  deleteMeeting: (id: string) => send("DELETE", `/meetings/${id}`).then((r) => json(r)),
  saveScratch: (id: string, lines: ScratchLine[]) =>
    send("PUT", `/meetings/${id}/scratch`, { lines }).then((r) => json(r)),
  exportMeeting: (id: string) =>
    send("POST", `/meetings/${id}/export`).then((r) => json<{ path: string }>(r)),
  audioUrl: (id: string, source: string) => `${BASE}/meetings/${id}/audio/${source}`,
};
