"""HTTP API consumed by the Tauri app (127.0.0.1 only)."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .config import PROJECT_ROOT, Settings
from .indexer import Indexer
from .meeting_notes import NotesWriter
from .meetings import MeetingManager
from .opener import open_hit
from .runtime import RemoteError, make_remote_client, make_runtime, normalize_remote_url
from .search import search, stream_answer
from .store import Store


class AskBody(BaseModel):
    question: str
    k: int = 6
    sources: list[str] | None = None


class OpenBody(BaseModel):
    path: str
    page: int = 0


class IndexBody(BaseModel):
    force: bool = False


class SettingsBody(BaseModel):
    corpus_dir: str | None = None
    chat_model: str | None = None


class MeetingStartBody(BaseModel):
    title: str = ""
    system_audio: bool = True
    mic_id: str | None = None
    language: str = ""  # empty: the profile's language
    live_answers: bool = False


class ScratchBody(BaseModel):
    lines: list[dict]


class MeetingPatchBody(BaseModel):
    title: str


class MeetingAskBody(BaseModel):
    question: str


class GatewayBody(BaseModel):
    enabled: bool | None = None
    regenerate_token: bool = False


class RemoteBody(BaseModel):
    url: str
    token: str
    corpus_dir: str | None = None


def probe_remote(url: str, token: str) -> dict:
    """Check that url is a Foresight server that accepts token; returns its health."""
    if not url:
        raise RemoteError("Enter the PC's address (shown in its Server panel, e.g. http://my-pc:8770).")
    if not token:
        raise RemoteError("Enter the password from the PC's Server panel.")
    with make_remote_client(token) as client:
        try:
            r = client.get(f"{url}/v1/health", timeout=8)
        except httpx.TimeoutException:
            raise RemoteError(f"The PC ({url}) doesn't answer. Turn it on, open Foresight and "
                              "enable Server.")
        except httpx.HTTPError as e:
            raise RemoteError(f"Can't connect to {url}: {e}")
    try:
        health = r.json()
    except ValueError:
        health = {}
    if r.status_code != 200 or not health.get("ok"):
        raise RemoteError(f"{url} doesn't look like a Foresight server (response {r.status_code}).")
    return health


def sse(events) -> StreamingResponse:
    """(event, data) pairs -> text/event-stream."""
    def frame(event: str, data) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def gen():
        try:
            for event, data in events:
                yield frame(event, data)
        except Exception as e:
            yield frame("error", str(e))
        yield frame("done-stream", {})

    return StreamingResponse(gen(), media_type="text/event-stream")


def create_app(s: Settings) -> FastAPI:
    rt = make_runtime(s)  # local models, or another PC's gateway when remote_url is set
    if rt.remote:  # scanned pages: OCR on the PC (Windows OCR is much faster)
        from . import ocr

        ocr.use_remote(rt.ocr_images)
    profile = s.load_profile()
    store = Store(s.data_dir, s.embed_dims)
    indexer = Indexer(s, store, rt, profile)
    meetings = MeetingManager(s, store, rt, profile)
    notes = NotesWriter(meetings)
    # Server mode only makes sense where the models run; it stays off unless enabled.
    gateway = None
    if not rt.remote:
        from .gateway import Gateway

        gateway = Gateway(s, rt, meetings)
    # starting | setup (remote with no PC yet) | waiting (PC unreachable) | ready | error
    boot = {"state": "starting", "error": ""}
    boot_thread: list[threading.Thread] = []

    def _wait_for_pc() -> bool:
        """Remote: wait until the PC answers. False if this machine has no PC set up yet."""
        while True:
            if not rt.configured:
                boot["state"], boot["error"] = "setup", ""
                return False
            h = rt.health(max_age=0)
            if h.get("ok"):
                return True
            boot["state"], boot["error"] = "waiting", h.get("error", "")
            time.sleep(3)

    def _boot():
        try:
            if rt.remote and not _wait_for_pc():
                return
            rt.start_embed()
            boot["state"], boot["error"] = "ready", ""
            rt.ensure_chat()  # load + warm Gemma 4 so the first question is fast
            if gateway:
                gateway.try_start()  # if it can't yet (Tailscale not up), it keeps retrying
            # Pick up anything that changed in the folder while the app was closed.
            indexer.run()
        except Exception as e:
            boot["state"], boot["error"] = "error", str(e)

    app = FastAPI(title="Foresight")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:1420", "http://tauri.localhost", "tauri://localhost"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _start_boot():
        if boot_thread and boot_thread[-1].is_alive():
            return  # still waiting for the PC: it picks up the new connection by itself
        boot_thread[:] = [threading.Thread(target=_boot, daemon=True, name="boot")]
        boot_thread[0].start()

    @app.on_event("startup")
    def _startup():
        _start_boot()

    @app.exception_handler(RemoteError)
    async def _remote_error(_request, e: RemoteError):
        return JSONResponse({"detail": str(e)}, status_code=503)

    @app.on_event("shutdown")
    def _shutdown():
        indexer.cancel()
        if meetings.state == "recording":
            meetings.stop()
        if gateway:
            gateway.stop()
        meetings.whisper.stop()
        rt.stop()

    def _require_ready():
        if boot["state"] != "ready":
            raise HTTPException(503, f"Engine not ready: {boot['state']} {boot['error']}".strip())

    @app.get("/api/status")
    def status():
        p = indexer.progress
        return {
            "boot": boot,
            "runtime": rt.status(),
            "remote": rt.remote,
            "gateway": None if not gateway else {
                "enabled": s.gateway_enabled, "running": gateway.running,
                "remote_class": bool(gateway.remote_class), "last_seen": gateway.last_seen,
            },
            "index": {**asdict(p), "rows": store.count(), "files": len(store.manifest)},
            "sources": store.sources(),
            "corpus_dir": str(s.corpus_dir),
            "corpus_exists": s.corpus_dir.is_dir(),
            "profile": {"subject": profile.subject, "language": profile.language,
                        "examples": profile.examples},
            "chat_models": sorted(f.name for f in (PROJECT_ROOT / "models").glob("gemma-4-*.gguf")),
        }

    @app.get("/api/search")
    def api_search(q: str, k: int = 12, source: list[str] | None = Query(None)):
        _require_ready()
        t = time.perf_counter()
        hits = search(store, rt, q, k, source)
        return {"hits": hits, "ms": round((time.perf_counter() - t) * 1000)}

    @app.post("/api/ask")
    def api_ask(body: AskBody):
        _require_ready()
        hits = search(store, rt, body.question, body.k, body.sources, max_per_source=3)

        def events():
            yield f"event: sources\ndata: {json.dumps(hits, ensure_ascii=False)}\n\n"
            try:
                for delta in stream_answer(rt, body.question, hits, profile):
                    yield f"event: token\ndata: {json.dumps(delta, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"event: error\ndata: {json.dumps(str(e))}\n\n"
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/api/index")
    def api_index(body: IndexBody):
        _require_ready()
        return {"started": indexer.start_background(force=body.force)}

    @app.post("/api/index/cancel")
    def api_index_cancel():
        indexer.cancel()
        return {"ok": True}

    @app.post("/api/open")
    def api_open(body: OpenBody):
        try:
            note = open_hit(s.corpus_dir, body.path, body.page)
        except FileNotFoundError:
            raise HTTPException(404, "File not found")
        return {"ok": True, "note": note}

    def _set_corpus(folder: str) -> None:
        new = Path(folder).expanduser()
        if not new.is_dir():
            raise HTTPException(400, "That folder doesn't exist")
        if new != s.corpus_dir:
            if indexer.running:
                raise HTTPException(409, "Wait for indexing to finish")
            s.corpus_dir = new
            # A different corpus means a fresh index.
            for rel in list(store.manifest):
                store.delete_path(rel)
            store.manifest.clear()
            store.save_manifest()
            if boot["state"] == "ready":
                indexer.start_background()  # otherwise boot indexes once the PC answers

    @app.post("/api/settings")
    def api_settings(body: SettingsBody):
        if body.corpus_dir:
            _set_corpus(body.corpus_dir)
        if body.chat_model and rt.remote:
            raise HTTPException(400, "In remote mode the PC picks the model")
        if body.chat_model:
            model = PROJECT_ROOT / "models" / body.chat_model
            if not model.exists():
                raise HTTPException(400, "Model not found")
            if model != s.chat_model:
                s.chat_model = model
                # Preload it. While a class is recorded the fast model stays (rt.override),
                # and stopping the class switches to this one.
                threading.Thread(target=rt.ensure_chat, daemon=True).start()
        s.save()
        return {"ok": True}

    # ------------------------------------------------------------ meetings

    @app.get("/api/audio/devices")
    def audio_devices():
        return meetings.devices()

    @app.get("/api/meetings")
    def meetings_list():
        return {"meetings": meetings.list(), "status": meetings.status()}

    @app.post("/api/meetings/start")
    def meetings_start(body: MeetingStartBody):
        _require_ready()
        try:
            return meetings.start(body.title, body.system_audio, body.mic_id,
                                  body.language or profile.language, body.live_answers)
        except (RuntimeError, ValueError) as e:
            raise HTTPException(409, str(e))

    @app.post("/api/meetings/stop")
    def meetings_stop():
        try:
            return meetings.stop()
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/meetings/events")
    def meetings_events():
        def events():
            q = meetings.bus.subscribe()
            try:
                yield "status", meetings.status()
                while True:
                    try:
                        yield q.get(timeout=10)
                    except queue.Empty:
                        yield "ping", {}
            finally:
                meetings.bus.unsubscribe(q)
        return sse(events())

    def _meeting_or_404(mid: str) -> dict:
        try:
            data = meetings.get(mid)
        except FileNotFoundError:
            raise HTTPException(404, "Class not found")
        refs = meetings.dir / mid / "notes_refs.json"
        data["refs"] = json.loads(refs.read_text(encoding="utf-8")) if refs.exists() else []
        return data

    @app.get("/api/meetings/{mid}")
    def meeting_get(mid: str):
        return _meeting_or_404(mid)

    @app.patch("/api/meetings/{mid}")
    def meeting_patch(mid: str, body: MeetingPatchBody):
        meetings.rename(mid, body.title)
        return meetings.meta(mid)

    @app.delete("/api/meetings/{mid}")
    def meeting_delete(mid: str):
        try:
            meetings.delete(mid)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.put("/api/meetings/{mid}/scratch")
    def meeting_scratch(mid: str, body: ScratchBody):
        meetings.save_scratch(mid, body.lines)
        return {"ok": True}

    @app.post("/api/meetings/{mid}/notes")
    def meeting_notes(mid: str):
        _require_ready()
        _meeting_or_404(mid)
        return sse(notes.generate(mid))

    @app.post("/api/meetings/{mid}/ask")
    def meeting_ask(mid: str, body: MeetingAskBody):
        _require_ready()
        return sse(notes.ask(mid, body.question))

    @app.post("/api/meetings/{mid}/export")
    def meeting_export(mid: str):
        try:
            path = notes.export(mid)
        except ValueError as e:
            raise HTTPException(400, str(e))
        indexer.start_background()  # make the new note searchable right away
        return {"path": str(path)}

    @app.get("/api/meetings/{mid}/audio/{source}")
    def meeting_audio(mid: str, source: str):
        path = meetings.dir / mid / f"audio_{source}.wav"
        if source not in ("class", "me") or not path.exists():
            raise HTTPException(404, "Audio not found")
        return FileResponse(path, media_type="audio/wav")

    # ------------------------------------------------------------ connection to the PC (remote)

    @app.get("/api/remote")
    def remote_get():
        if not rt.remote:
            raise HTTPException(404, "This computer runs the models itself")
        return {"url": s.remote_url, "token": s.remote_token, "corpus_dir": str(s.corpus_dir),
                "corpus_exists": s.corpus_dir.is_dir(), "default_port": s.gateway_port}

    @app.post("/api/remote/test")
    def remote_test(body: RemoteBody):
        url = normalize_remote_url(body.url, s.gateway_port)
        try:
            h = probe_remote(url, body.token.strip())
        except RemoteError as e:
            return {"ok": False, "url": url, "error": str(e)}
        return {"ok": True, "url": url, "chat_model": h.get("chat_model", ""),
                "device": h.get("device", "")}

    @app.post("/api/remote")
    def remote_set(body: RemoteBody):
        if not rt.remote:
            raise HTTPException(404, "This computer runs the models itself")
        url, token = normalize_remote_url(body.url, s.gateway_port), body.token.strip()
        try:
            probe_remote(url, token)
        except RemoteError as e:
            raise HTTPException(400, str(e))
        if (url, token) != (s.remote_url, s.remote_token) and meetings.state != "idle":
            raise HTTPException(409, "Stop the class being recorded before switching PCs")
        if body.corpus_dir:
            _set_corpus(body.corpus_dir)
        s.remote_url, s.remote_token = url, token
        s.save()
        rt.connect(url, token)
        if boot["state"] != "ready":
            _start_boot()
        return {"ok": True, "url": url}

    # ------------------------------------------------------------ server mode (local only)

    @app.get("/api/gateway")
    def gateway_status():
        if not gateway:
            raise HTTPException(404, "This computer uses another PC's models")
        return gateway.status()

    @app.post("/api/gateway")
    def gateway_set(body: GatewayBody):
        if not gateway:
            raise HTTPException(404, "This computer uses another PC's models")
        if body.regenerate_token:
            gateway.regenerate_token()  # old token stops working immediately
        if body.enabled is not None:
            try:
                gateway.set_enabled(body.enabled)
            except Exception as e:
                raise HTTPException(409, str(e))
        return gateway.status()

    return app


def _exit_with_parent() -> None:
    """macOS has no job objects: if the app that started us dies, quit too."""
    parent = int(os.environ.get("FORESIGHT_PARENT_PID") or 0)
    if not parent or sys.platform == "win32":
        return

    def watch():
        while os.getppid() == parent:
            time.sleep(2)
        os._exit(0)

    threading.Thread(target=watch, daemon=True, name="parent-watch").start()


def run(s: Settings) -> None:
    _exit_with_parent()
    uvicorn.run(create_app(s), host="127.0.0.1", port=s.api_port, log_level="warning")
