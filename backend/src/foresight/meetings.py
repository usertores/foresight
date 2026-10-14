"""Live class recording: capture audio, transcribe with Whisper, surface related material.

One recording at a time. Each audio source (the computer's output = the online class, and
optionally the microphone) runs in its own thread: capture -> aligned WAV on disk ->
voice-activity segmenter. Finished segments go to a single Whisper worker; the results are
pushed to the UI through an in-process event bus (served as SSE).
"""

from __future__ import annotations

import base64
import io
import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import wave
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

# Imported at module load, i.e. in the main thread, which lives as long as the app: soundcard
# initialises COM in the importing thread, and that keeps COM's multithreaded apartment alive
# for the API's pooled threads (see _com_init).
import soundcard as sc

from .config import Settings
from .profile import Profile
from .runtime import CREATE_NO_WINDOW, FAST_CHAT_MODEL, Runtime
from .search import search
from .store import Store

SR = 16000
FRAME = 480  # 30 ms, the frame size webrtcvad expects
BLOCK = FRAME * 10  # 300 ms per capture read

# Audio sources: the computer's output (the class) and the user's microphone.
SPEAKERS = {"class": "Class", "me": "Me"}

# Phrases Whisper tends to hallucinate on silence or music (English and Spanish).
HALLUCINATIONS = re.compile(
    r"amara\.org|subtítulos (realizados|por)|gracias por ver|suscríbete|thanks for watching|"
    r"^\W*(gracias|música|music)\W*$", re.I)


def _com_init() -> None:
    """Join this thread to COM's multithreaded apartment before using soundcard.

    soundcard initialises COM only in the thread that imports it; if that thread exits,
    other threads fail with 0x800401f0 (CO_E_NOTINITIALIZED). The module-level import keeps
    the main thread's apartment alive; this makes each worker thread explicit about it.
    Must run after soundcard is imported: its own init treats S_FALSE as an error.
    """
    if sys.platform != "win32":
        return
    import ctypes

    ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED


def fmt_ts(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


@dataclass
class Segment:
    id: int
    source: str  # "class" | "me"
    t0: float
    t1: float
    text: str


# ------------------------------------------------------------------ event bus

class EventBus:
    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: str, data) -> None:
        with self._lock:
            for q in self._subs:
                try:
                    q.put_nowait((event, data))
                except queue.Full:
                    pass


# ------------------------------------------------------------------ whisper

class Whisper:
    def __init__(self, s: Settings, log_dir: Path):
        self.s = s
        self.url = f"http://127.0.0.1:{s.whisper_port}"
        self.log_path = log_dir / "whisper.log"
        self.proc: subprocess.Popen | None = None
        self.http = httpx.Client(timeout=120)

    def healthy(self) -> bool:
        try:
            return httpx.get(self.url, timeout=1).status_code == 200
        except Exception:
            return False

    def start(self, timeout: float = 90) -> None:
        if self.healthy():
            return
        log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [str(self.s.whisper_server), "-m", str(self.s.whisper_model), "--host", "127.0.0.1",
             "--port", str(self.s.whisper_port), "-nt"],
            stdout=log, stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW,
            cwd=self.s.whisper_server.parent,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"whisper-server exited; see {self.log_path}")
            if self.healthy():
                return
            time.sleep(0.4)
        raise TimeoutError("whisper-server did not start in time")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def transcribe(self, audio: np.ndarray, language: str, prompt: str) -> str:
        return self.transcribe_wav(to_wav(audio), language, prompt)

    def transcribe_wav(self, wav: bytes, language: str, prompt: str) -> str:
        data = {"temperature": "0.0", "response_format": "json", "prompt": prompt}
        if language and language != "auto":
            data["language"] = language
        def post():
            return self.http.post(f"{self.url}/inference",
                                  files={"file": ("a.wav", wav, "audio/wav")}, data=data)

        try:
            r = post()
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
            # whisper-server died mid-class: restart it and retry this segment once, so the
            # rest of the class keeps being transcribed.
            self.start()
            r = post()
        r.raise_for_status()
        return " ".join(r.json().get("text", "").split())


def to_wav(audio: np.ndarray) -> bytes:
    """16 kHz mono float samples -> 16-bit WAV bytes."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


class RemoteWhisper:
    """Whisper on the PC, used from another machine through the gateway.

    start/stop open and close a "class session" on the PC: it loads Whisper and switches to
    the fast Gemma model, exactly as a local class would.
    """

    def __init__(self, rt):
        self.rt = rt
        self.session: str | None = None

    def healthy(self) -> bool:
        return bool(self.rt.health().get("whisper"))

    def start(self) -> None:
        r = self.rt.http.post(f"{self.rt.base}/v1/class/start", timeout=180)
        if r.status_code == 409:
            raise RuntimeError(r.json().get("detail", "The PC is busy"))
        r.raise_for_status()
        self.session = r.json()["session"]

    def stop(self) -> None:
        if self.session:
            try:
                self.rt.http.post(f"{self.rt.base}/v1/class/stop", json={"session": self.session})
            except Exception:
                pass  # the PC also closes idle sessions on its own
            self.session = None

    def transcribe(self, audio: np.ndarray, language: str, prompt: str) -> str:
        r = self.rt.http.post(f"{self.rt.base}/v1/transcribe", json={
            "session": self.session, "language": language, "prompt": prompt,
            "audio_b64": base64.b64encode(to_wav(audio)).decode("ascii"),
        }, timeout=120)
        r.raise_for_status()
        return r.json()["text"]


# ------------------------------------------------------------------ segmentation

class Segmenter:
    """webrtcvad-based: start on sustained voice, end on ~0.7 s of silence or at 25 s."""

    def __init__(self, source: str, emit):
        import webrtcvad

        self.vad = webrtcvad.Vad(2)
        self.source, self.emit = source, emit
        self._reset()
        self.seq = 0

    def _reset(self):
        self.pre: deque[tuple[int, np.ndarray, bool]] = deque(maxlen=10)
        self.buf: list[np.ndarray] = []
        self.start: int | None = None
        self.silence = 0
        self.voiced = 0
        self.last_interim = 0.0

    def push(self, frame: np.ndarray, idx: int) -> None:
        pcm = (np.clip(frame, -1, 1) * 32767).astype(np.int16).tobytes()
        voiced = float(np.sqrt(np.mean(frame ** 2))) > 0.002 and self.vad.is_speech(pcm, SR)
        if self.start is None:
            self.pre.append((idx, frame, voiced))
            if sum(v for *_, v in list(self.pre)[-6:]) >= 4:
                self.start = self.pre[0][0]
                self.buf = [f for _, f, _ in self.pre]
                self.voiced = sum(v for *_, v in self.pre)
                self.pre.clear()
                self.seq += 1
            return
        self.buf.append(frame)
        if voiced:
            self.silence = 0
            self.voiced += 1
        else:
            self.silence += 1
        dur = len(self.buf) * FRAME / SR
        if (self.silence >= 23 and (dur >= 2.0 or self.silence >= 60)) or dur >= 25:
            self.flush()
        elif dur - self.last_interim >= 3.5 and self.voiced * FRAME / SR >= 1.0:
            self.last_interim = dur
            self.emit("interim", self.source, self.seq, self.start, np.concatenate(self.buf))

    def flush(self) -> None:
        if self.start is not None and self.voiced * FRAME / SR >= 0.6:
            keep = max(len(self.buf) - max(self.silence - 7, 0), 1)  # trim trailing silence
            audio = np.concatenate(self.buf[:keep])
            self.emit("final", self.source, self.seq, self.start, audio)
        self._reset()


# ------------------------------------------------------------------ capture

class Capture(threading.Thread):
    """Reads one device, writes a timeline-aligned WAV and feeds the segmenter."""

    def __init__(self, session: "LiveSession", source: str, device, wav_path: Path):
        super().__init__(daemon=True, name=f"capture-{source}")
        self.session, self.source, self.device = session, source, device
        self.wav = wave.open(str(wav_path), "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(SR)
        self.written = 0
        self.level = 0.0
        self.seg = Segmenter(source, session.on_audio_segment)
        self.error = ""
        self._pending = np.zeros(0, dtype=np.float32)

    def _write(self, x: np.ndarray) -> None:
        self.wav.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
        # feed the segmenter frame by frame with absolute sample indices
        data = np.concatenate([self._pending, x])
        n = len(data) // FRAME
        base = self.written - len(self._pending)
        for i in range(n):
            self.seg.push(data[i * FRAME:(i + 1) * FRAME], base + i * FRAME)
        self._pending = data[n * FRAME:]
        self.written += len(x)

    def run(self) -> None:
        try:
            _com_init()
            # CoreAudio caps a device's buffer at 4096 frames; record() still returns BLOCK.
            blocksize = None if sys.platform == "darwin" else BLOCK
            with self.device.recorder(samplerate=SR, channels=1, blocksize=blocksize) as rec:
                while not self.session.stopping.is_set():
                    block = rec.record(numframes=BLOCK)[:, 0].astype(np.float32)
                    # Loopback (and the Mac's system audio) delivers nothing while the computer
                    # is silent: pad the gap so the WAV and the timestamps stay aligned with
                    # wall-clock time.
                    expected = int((time.monotonic() - self.session.t_start) * SR) - len(block)
                    gap = expected - self.written
                    if gap > SR // 2:
                        self._write(np.zeros(gap, dtype=np.float32))
                    self.level = float(np.sqrt(np.mean(block ** 2))) if len(block) else 0.0
                    self._write(block)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self.session.bus.publish("error", {"message": f"{SPEAKERS[self.source]} audio: {self.error}"})
        finally:
            self.seg.flush()
            self.wav.close()


# ------------------------------------------------------------------ live session

@dataclass
class Card:
    t: float
    kind: str  # "context" | "answer"
    hits: list[dict] = field(default_factory=list)
    question: str = ""
    answer: str = ""


class LiveSession:
    def __init__(self, mgr: "MeetingManager", meeting_id: str, sources: dict[str, object],
                 language: str, live_answers: bool):
        self.mgr, self.id = mgr, meeting_id
        self.dir = mgr.dir / meeting_id
        self.bus = mgr.bus
        self.language = language
        self.live_answers = live_answers
        self.stopping = threading.Event()
        self.t_start = time.monotonic()
        self.segments: list[Segment] = []
        self.cards: list[Card] = []
        self.shown: set[tuple[str, int]] = set()
        self._jobs: queue.PriorityQueue = queue.PriorityQueue()
        self._job_seq = 0
        self._finals_done: dict[str, int] = {}
        self._lock = threading.Lock()
        self._last_context = 0.0
        self._context_cursor = 0
        self._answer_cursor = 0
        self.captures = [Capture(self, src, dev, self.dir / f"audio_{src}.wav")
                         for src, dev in sources.items()]
        self.worker = threading.Thread(target=self._transcribe_loop, daemon=True, name="whisper")
        self.assist = threading.Thread(target=self._assist_loop, daemon=True, name="assist")

    def start(self) -> None:
        self.t_start = time.monotonic()
        for c in self.captures:
            c.start()
        self.worker.start()
        self.assist.start()
        threading.Thread(target=self._levels_loop, daemon=True).start()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.t_start

    # -------------------------------------------------------------- audio -> jobs

    def on_audio_segment(self, kind: str, source: str, seq: int, start_idx: int, audio: np.ndarray):
        with self._lock:
            self._job_seq += 1
            prio = 0 if kind == "final" else 1
            self._jobs.put((prio, self._job_seq, kind, source, seq, start_idx, audio))

    def _prompt(self, source: str) -> str:
        prev = " ".join(s.text for s in self.segments[-3:] if s.source == source)
        return (self.mgr.profile.whisper_prompt() + " " + prev[-300:]).strip()

    def _transcribe_loop(self) -> None:
        whisper = self.mgr.whisper
        while True:
            try:
                prio, _, kind, source, seq, start_idx, audio = self._jobs.get(timeout=0.3)
            except queue.Empty:
                if self.stopping.is_set() and all(not c.is_alive() for c in self.captures):
                    break
                continue
            if kind == "interim":
                # Skip stale partials: a final for this utterance exists, or work is queued.
                if self._finals_done.get(source, 0) >= seq or not self._jobs.empty():
                    continue
            try:
                text = whisper.transcribe(audio, self.language, self._prompt(source))
            except Exception as e:
                self.bus.publish("error", {"message": f"Whisper: {e}"})
                continue
            if HALLUCINATIONS.search(text):
                text = ""
            t0 = start_idx / SR
            if kind == "interim":
                if text:
                    self.bus.publish("interim", {"source": source, "t0": t0, "text": text})
                continue
            self._finals_done[source] = max(self._finals_done.get(source, 0), seq)
            self.bus.publish("interim", {"source": source, "t0": t0, "text": ""})
            if not text:
                continue
            seg = Segment(id=len(self.segments) + 1, source=source, t0=round(t0, 2),
                          t1=round(t0 + len(audio) / SR, 2), text=text)
            self.segments.append(seg)
            with open(self.dir / "transcript.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(seg), ensure_ascii=False) + "\n")
            self.bus.publish("segment", asdict(seg))

    def _levels_loop(self) -> None:
        while not self.stopping.is_set():
            self.bus.publish("level", {
                "elapsed": round(self.elapsed, 1),
                "levels": {c.source: round(min(1.0, c.level * 8), 3) for c in self.captures},
            })
            time.sleep(0.25)

    # -------------------------------------------------------------- live assistance

    def _assist_loop(self) -> None:
        while not (self.stopping.is_set() and not self.worker.is_alive()):
            time.sleep(1.5)
            try:
                self._maybe_context()
                if self.live_answers:
                    self._maybe_answer()
            except Exception as e:
                self.bus.publish("error", {"message": f"Assistant: {e}"})

    def _maybe_context(self) -> None:
        new = self.segments[self._context_cursor:]
        if not new or time.monotonic() - self._last_context < 12:
            return
        recent = " ".join(s.text for s in self.segments[-8:]).split()[-70:]
        if len(recent) < 20:
            return
        self._context_cursor = len(self.segments)
        self._last_context = time.monotonic()
        hits = search(self.mgr.store, self.mgr.rt, " ".join(recent), k=5, max_per_file=1)
        fresh = [h for h in hits if (h["similarity"] or 0) >= 0.74
                 and (h["path"], h["page"]) not in self.shown][:2]
        if not fresh:
            return
        for h in fresh:
            self.shown.add((h["path"], h["page"]))
        card = Card(t=round(self.elapsed, 1), kind="context", hits=[_slim(h) for h in fresh])
        self._add_card(card)

    def _maybe_answer(self) -> None:
        new = self.segments[self._answer_cursor:]
        self._answer_cursor = len(self.segments)
        for seg in new:
            q = _question_in(seg.text)
            if not q:
                continue
            hits = search(self.mgr.store, self.mgr.rt, q, k=4, max_per_source=2)
            answer = self.mgr.short_answer(q, hits)
            if answer:
                self._add_card(Card(t=seg.t0, kind="answer", question=q, answer=answer,
                                    hits=[_slim(h) for h in hits]))

    def _add_card(self, card: Card) -> None:
        self.cards.append(card)
        (self.dir / "cards.json").write_text(
            json.dumps([asdict(c) for c in self.cards], ensure_ascii=False), encoding="utf-8")
        self.bus.publish("card", asdict(card))

    # -------------------------------------------------------------- stop

    def stop(self, timeout: float = 120) -> None:
        self.stopping.set()
        for c in self.captures:
            c.join(5)
        self.worker.join(timeout)
        self.assist.join(10)


def _repair_wav(path: Path) -> float:
    """Rewrite the header of a WAV left unclosed (sizes still 0); returns its length in s."""
    data = path.read_bytes()
    pcm = data[44:]
    pcm = pcm[: len(pcm) - len(pcm) % 2]
    try:
        with wave.open(str(path), "rb") as w:
            if w.getnframes() * 2 == len(pcm):
                return len(pcm) / 2 / SR
    except Exception:
        pass
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    return len(pcm) / 2 / SR


# Whisper often drops question marks, so also recognise how questions to the class usually
# start, in English and Spanish. ("por qué" is left out: Whisper often writes it for
# "porque", which starts answers.)
_QUESTION_START = re.compile(
    r"^((vale|bueno|entonces|y|so|ok|okay),?\s+)?"
    r"(alguien sabe|sab[eé]is|sabr[ií]ais|os acord[aá]is|qu[eé] (es|son|pasa|ocurre)|"
    r"c[oó]mo (se|es|calcul)|cu[aá]l(es)? (es|son|ser[ií]a)|"
    r"what (is|are|happens)|why (do|does|is)|how (do|does|is|would))\b", re.I)


def _question_in(text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    for s in reversed(sentences):
        s = s.strip()
        if len(s.split()) >= 5 and ("?" in s or "¿" in s or _QUESTION_START.match(s.lstrip("¿¡ "))):
            return s
    return ""


def _slim(h: dict) -> dict:
    return {k: h[k] for k in ("path", "title", "source", "section", "page", "text", "similarity")}


# ------------------------------------------------------------------ manager

class MeetingManager:
    def __init__(self, s: Settings, store: Store, rt: Runtime, profile: Profile):
        self.s, self.store, self.rt, self.profile = s, store, rt, profile
        self.dir = s.data_dir / "meetings"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.bus = EventBus()
        # Remote (e.g. a Mac): Whisper runs on the PC behind its gateway.
        self.whisper = RemoteWhisper(rt) if rt.remote else Whisper(s, rt.log_dir)
        # Set by the gateway on the PC: returns a reason when a class from another machine
        # is using Whisper and the GPU, so a local class must wait.
        self.busy_elsewhere = lambda: ""
        self.live: LiveSession | None = None
        self.state = "idle"  # idle | starting | recording | stopping
        self._lock = threading.Lock()
        self._recover()

    def _recover(self) -> None:
        """Close recordings interrupted by a crash or by quitting the app mid-class."""
        for meta_path in self.dir.glob("*/meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if meta.get("status") != "recording":
                continue
            d = meta_path.parent
            length = 0.0
            for wav in d.glob("audio_*.wav"):
                length = max(length, _repair_wav(wav))
            segs = self.segments(meta["id"])
            meta["duration"] = round(max(length, segs[-1]["t1"] if segs else 0))
            meta["status"] = "stopped"
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    # -------------------------------------------------------------- devices

    @staticmethod
    def devices() -> dict:
        _com_init()

        spk = sc.default_speaker()
        mic = sc.default_microphone()
        return {
            "speaker": spk.name,
            # CoreAudio ids are numbers; the app sends them back as strings.
            "mics": [{"id": str(m.id), "name": m.name, "default": m.name == mic.name}
                     for m in sc.all_microphones()],
        }

    # -------------------------------------------------------------- persistence

    def _meta_path(self, mid: str) -> Path:
        return self.dir / mid / "meta.json"

    def meta(self, mid: str) -> dict:
        return json.loads(self._meta_path(mid).read_text(encoding="utf-8"))

    def save_meta(self, mid: str, meta: dict) -> None:
        self._meta_path(mid).write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    def list(self) -> list[dict]:
        out = []
        for p in self.dir.glob("*/meta.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
        return sorted(out, key=lambda m: m["created"], reverse=True)

    def segments(self, mid: str) -> list[dict]:
        if self.live and self.live.id == mid:
            return [asdict(s) for s in self.live.segments]
        path = self.dir / mid / "transcript.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def get(self, mid: str) -> dict:
        d = self.dir / mid
        read = lambda name, default: (json.loads((d / name).read_text(encoding="utf-8"))
                                      if (d / name).exists() else default)
        return {
            "meta": self.meta(mid),
            "segments": self.segments(mid),
            "scratch": read("scratch.json", []),
            "cards": read("cards.json", []),
            "notes": (d / "notes.md").read_text(encoding="utf-8") if (d / "notes.md").exists() else "",
            "audio": sorted(p.stem.removeprefix("audio_") for p in d.glob("audio_*.wav")),
        }

    def save_scratch(self, mid: str, lines: list[dict]) -> None:
        (self.dir / mid / "scratch.json").write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")

    def rename(self, mid: str, title: str) -> None:
        meta = self.meta(mid)
        meta["title"] = title.strip() or meta["title"]
        self.save_meta(mid, meta)

    def delete(self, mid: str) -> None:
        if self.live and self.live.id == mid:
            raise RuntimeError("Can't delete the class that is being recorded")
        shutil.rmtree(self.dir / mid, ignore_errors=True)

    # -------------------------------------------------------------- recording

    def start(self, title: str, system_audio: bool, mic_id: str | None, language: str,
              live_answers: bool) -> dict:
        _com_init()

        with self._lock:
            if self.state != "idle":
                raise RuntimeError("A recording is already in progress")
            if reason := self.busy_elsewhere():
                raise RuntimeError(reason)
            self.state = "starting"
        try:
            sources = {}
            if system_audio and sys.platform == "darwin":
                from . import macaudio

                macaudio.check_permission()
                sources["class"] = macaudio.SystemAudio()
            elif system_audio:
                spk = sc.default_speaker()
                sources["class"] = sc.get_microphone(id=str(spk.name), include_loopback=True)
            if mic_id:
                sources["me"] = next((m for m in sc.all_microphones() if str(m.id) == mic_id),
                                     None) or sc.get_microphone(id=mic_id)
            if not sources:
                raise ValueError("Pick at least one audio source")

            # Live answers must be quick and Whisper needs VRAM: record with the fast model
            # whatever the selector says. Swap before loading Whisper so E4B's VRAM is free.
            # (Remote: the PC does this itself when the class session opens.)
            fast = self.s.chat_model.parent / FAST_CHAT_MODEL
            if not self.rt.remote and fast.exists():
                self.rt.override = fast
                if self.rt.chat_loaded() != fast.stem:
                    self.bus.publish("status", {"state": "starting",
                                                "message": "Loading Gemma 4 E2B for the class…"})
                self.rt.ensure_chat()
            self.bus.publish("status", {"state": "starting", "message":
                                        "Getting Whisper ready on the PC…" if self.rt.remote
                                        else "Loading Whisper…"})
            self.whisper.start()
            self.rt.start_embed()

            now = datetime.now()
            mid = now.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
            (self.dir / mid).mkdir(parents=True)
            meta = {
                "id": mid, "title": title.strip() or f"Class {now:%Y-%m-%d %H:%M}",
                "created": now.isoformat(timespec="seconds"), "duration": 0,
                "language": language, "sources": list(sources), "status": "recording",
                "live_answers": live_answers,
            }
            self.save_meta(mid, meta)
            self.live = LiveSession(self, mid, sources, language, live_answers)
            self.live.start()
            self.state = "recording"
            self.bus.publish("status", {"state": "recording", "id": mid})
            return meta
        except Exception:
            self.state = "idle"
            self.rt.override = None
            self.whisper.stop()  # don't leave Whisper (or a session on the PC) behind
            raise

    def stop(self) -> dict:
        with self._lock:
            if self.state != "recording" or not self.live:
                raise RuntimeError("No recording in progress")
            self.state = "stopping"
        live = self.live
        self.bus.publish("status", {"state": "stopping", "id": live.id,
                                    "message": "Finishing the transcription…"})
        live.stop()
        meta = self.meta(live.id)
        meta["duration"] = round(live.elapsed)
        meta["status"] = "stopped"
        self.save_meta(live.id, meta)
        self.live = None
        self.whisper.stop()  # free ~0.7 GB of VRAM until the next class
        # Back to the model the user picked (it writes the notes). Loaded in the background;
        # the notes request waits on the same lock, so it never sees a half-swapped server.
        if not self.rt.remote:
            self.rt.override = None
            threading.Thread(target=self.rt.ensure_chat, daemon=True).start()
        self.state = "idle"
        self.bus.publish("status", {"state": "idle", "id": meta["id"]})
        return meta

    def status(self) -> dict:
        live = self.live
        return {
            "state": self.state,
            "id": live.id if live else None,
            "elapsed": round(live.elapsed, 1) if live else 0,
            "whisper_ready": self.whisper.healthy() if self.state != "idle" else False,
        }

    # -------------------------------------------------------------- gemma helpers

    def short_answer(self, question: str, hits: list[dict]) -> str:
        """Brief answer for a question asked aloud in class (live assistance)."""
        self.rt.ensure_chat()
        ctx = "\n\n".join(f"[{i}] ({h['title']}, p. {h['page']})\n{h['text']}"
                          for i, h in enumerate(hits, 1))
        r = self.rt.http.post(f"{self.rt.chat_url}/v1/chat/completions", json={
            "messages": [
                {"role": "system", "content": "Answer in 2-3 sentences, in the language of the "
                 "question, using only the excerpts. Cite with [n]. If the answer is not in the "
                 "excerpts, reply exactly: NO_ANSWER."},
                {"role": "user", "content": f"Excerpts:\n{ctx}\n\nQuestion heard in class: {question}"},
            ],
            "temperature": 0.2, "max_tokens": 220,
            "chat_template_kwargs": {"enable_thinking": False},
        })
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
        return "" if "NO_ANSWER" in text else text
