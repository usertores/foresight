"""Server mode: lend this PC's models to the user's other computers (e.g. a Mac).

A second, separate HTTP server that only exposes the models (embeddings, Gemma, Whisper),
never the app's own API (which can open files and programs). It is off by default, binds to
this PC's Tailscale address only, and every request needs the token shown in the app.
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import anyio
import numpy as np
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import ocr
from .config import Settings
from .meetings import FAST_CHAT_MODEL, MeetingManager
from .runtime import CREATE_NO_WINDOW, Runtime

REMOTE_CLASS_IDLE_S = 300  # close a remote class that sent no audio for 5 min (client gone)
RETRY_S = 30  # while enabled but not serving (e.g. Tailscale not up yet after a reboot)
MAX_EMBED_BATCH = 64
MAX_OCR_BATCH = 8
MAX_OCR_BYTES = 24 * 1024 * 1024


# ------------------------------------------------------------------ tailscale

def _tailscale_exe() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    default = Path(r"C:\Program Files\Tailscale\tailscale.exe")
    return str(default) if default.exists() else None


def tailscale_status() -> dict:
    """{installed, running, ip, name, error} for this machine."""
    exe = _tailscale_exe()
    if not exe:
        return {"installed": False, "running": False}
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True,
                             timeout=8, creationflags=CREATE_NO_WINDOW).stdout
        st = json.loads(out)
    except Exception as e:
        return {"installed": True, "running": False, "error": str(e)}
    me = st.get("Self") or {}
    ips = [ip for ip in me.get("TailscaleIPs") or [] if "." in ip]
    return {
        "installed": True,
        "running": st.get("BackendState") == "Running",
        "state": st.get("BackendState", ""),
        "ip": ips[0] if ips else "",
        "name": (me.get("DNSName") or "").rstrip("."),
        "short_name": (me.get("HostName") or "").lower(),
    }


# ------------------------------------------------------------------ keep awake

class KeepAwake:
    """Stop Windows from idle-sleeping while the server mode is on (closing the lid still
    follows the user's power settings). The request lives as long as its thread."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if sys.platform != "win32" or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()

        def run():
            import ctypes

            es_continuous, es_system_required = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)
            self._stop.wait()
            ctypes.windll.kernel32.SetThreadExecutionState(es_continuous)

        self._thread = threading.Thread(target=run, daemon=True, name="keep-awake")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None


# ------------------------------------------------------------------ gateway

class Gateway:
    def __init__(self, s: Settings, rt: Runtime, meetings: MeetingManager):
        self.s, self.rt, self.meetings = s, rt, meetings
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.address = ""
        self.error = ""
        self.last_seen = 0.0
        self.requests = 0
        self.remote_class: dict | None = None  # {"session", "started", "last"}
        self._class_lock = threading.Lock()
        self._life = threading.RLock()  # start/stop/retry never overlap
        self._awake = KeepAwake()
        self.app = self._build_app()
        meetings.busy_elsewhere = self._busy_reason
        threading.Thread(target=self._watchdog, daemon=True, name="gateway-watchdog").start()

    # -------------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return bool(self.server and self.server.started and self.thread and self.thread.is_alive())

    def _host(self) -> str:
        if self.s.gateway_host != "tailscale":
            return self.s.gateway_host
        ts = tailscale_status()
        if not ts.get("installed"):
            raise RuntimeError("Tailscale isn't installed on this PC.")
        if not ts.get("running") or not ts.get("ip"):
            raise RuntimeError("Tailscale isn't connected: open it and sign in.")
        return ts["ip"]

    def start(self) -> None:
        with self._life:
            self._start()

    def _start(self) -> None:
        if self.running:
            return
        self.error = ""
        if not self.s.gateway_token:
            self.s.gateway_token = secrets.token_urlsafe(24)
            self.s.save()
        host = self._host()
        config = uvicorn.Config(self.app, host=host, port=self.s.gateway_port,
                                log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="gateway")
        self.thread.start()
        deadline = time.time() + 8
        while time.time() < deadline and not self.server.started and self.thread.is_alive():
            time.sleep(0.1)
        if not self.running:
            self.server = None
            raise RuntimeError(f"Couldn't open port {self.s.gateway_port} on {host}.")
        self.address = f"{host}:{self.s.gateway_port}"
        self._awake.start()

    def stop(self) -> None:
        with self._life:
            self._end_remote_class()
            if self.server:
                self.server.should_exit = True
            if self.thread:
                self.thread.join(5)
            self.server = self.thread = None
            self.address = ""
            self._awake.stop()

    def set_enabled(self, enabled: bool) -> None:
        with self._life:
            if enabled:
                try:
                    self.start()
                except Exception as e:
                    self.error = str(e)
                    raise  # stays off: only a server that actually started is remembered
            else:
                self.stop()
            self.s.gateway_enabled = enabled
            self.s.save()

    def try_start(self) -> None:
        """Start if enabled and not serving; never raises (used at boot and by the retry loop).

        After a reboot the app may open before Tailscale has connected; instead of making the
        user switch it off and on, keep trying every RETRY_S seconds while it is enabled.
        """
        with self._life:
            if not self.s.gateway_enabled or self.running:
                return
            try:
                self.start()
            except Exception as e:
                self.error = f"{e} Retrying every {RETRY_S} s."

    def regenerate_token(self) -> None:
        self.s.gateway_token = secrets.token_urlsafe(24)
        self.s.save()

    def status(self) -> dict:
        ts = tailscale_status()
        port = self.s.gateway_port
        return {
            "enabled": self.s.gateway_enabled,
            "running": self.running,
            "error": self.error,
            "address": self.address,
            "url_name": f"http://{ts['short_name']}:{port}" if ts.get("short_name") else "",
            "url_ip": f"http://{ts['ip']}:{port}" if ts.get("ip") else "",
            "token": self.s.gateway_token,
            "tailscale": ts,
            "last_seen": self.last_seen,
            "requests": self.requests,
            "remote_class": bool(self.remote_class),
        }

    # -------------------------------------------------------------- remote classes

    def _busy_reason(self) -> str:
        return "Another computer is recording a class with this PC; wait for it to finish." \
            if self.remote_class else ""

    def _end_remote_class(self) -> None:
        with self._class_lock:
            if not self.remote_class:
                return
            self.remote_class = None
            if self.meetings.state == "idle":
                self.meetings.whisper.stop()
                self.rt.override = None
                threading.Thread(target=self.rt.ensure_chat, daemon=True).start()

    def _watchdog(self) -> None:
        while True:
            time.sleep(RETRY_S)
            rc = self.remote_class
            if rc and time.time() - rc["last"] > REMOTE_CLASS_IDLE_S:
                self._end_remote_class()
            self.try_start()

    # -------------------------------------------------------------- HTTP app

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Foresight gateway", docs_url=None, redoc_url=None,
                      openapi_url=None)
        rt, mgr = self.rt, self.meetings

        def auth(authorization: str = Header("")) -> None:
            token = self.s.gateway_token
            if not token or not hmac.compare_digest(authorization.encode(),
                                                    f"Bearer {token}".encode()):
                raise HTTPException(401, "Wrong server password")
            self.last_seen = time.time()
            self.requests += 1

        guarded = [Depends(auth)]

        @app.get("/v1/health", dependencies=guarded)
        def health():
            st = rt.status()
            return {
                "ok": True,
                "device": st["device"],
                "embed": st["embed"],
                "chat_model": st["chat_model"],
                "chat_selected": st["chat_selected"],
                "whisper": mgr.whisper.healthy(),
                "pc_recording": mgr.state != "idle",
                "remote_class": bool(self.remote_class),
                "ocr": ocr.available(),
            }

        @app.post("/v1/ocr", dependencies=guarded)
        def ocr_images(body: dict):
            images = body.get("images") or []
            if not isinstance(images, list) or not 0 < len(images) <= MAX_OCR_BATCH:
                raise HTTPException(400, f"Send between 1 and {MAX_OCR_BATCH} images")
            if not ocr.available():
                raise HTTPException(503, "This PC has no OCR available")
            pngs = [base64.b64decode(str(img)) for img in images]
            if sum(len(p) for p in pngs) > MAX_OCR_BYTES:
                raise HTTPException(400, "Images too large")
            return {"lines": ocr.ocr_images(pngs)}

        @app.post("/v1/embed", dependencies=guarded)
        def embed(body: dict):
            texts = body.get("input") or []
            if not isinstance(texts, list) or not 0 < len(texts) <= MAX_EMBED_BATCH:
                raise HTTPException(400, f"Send between 1 and {MAX_EMBED_BATCH} texts")
            rt.start_embed()
            v = np.ascontiguousarray(rt._embed([str(t) for t in texts]), dtype=np.float32)
            return {"count": v.shape[0], "dims": v.shape[1],
                    "b64": base64.b64encode(v.tobytes()).decode("ascii")}

        @app.post("/v1/chat/completions", dependencies=guarded)
        async def chat(request: Request):
            body = await request.json()
            # Run the (possibly slow) model check/swap off the event loop.
            await anyio.to_thread.run_sync(rt.ensure_chat)
            url = f"{rt.chat_url}/v1/chat/completions"
            if not body.get("stream"):
                r = await anyio.to_thread.run_sync(lambda: rt.http.post(url, json=body))
                return JSONResponse(r.json(), status_code=r.status_code)

            def relay():
                with rt.http.stream("POST", url, json=body) as r:
                    for chunk in r.iter_raw():
                        yield chunk

            return StreamingResponse(relay(), media_type="text/event-stream")

        @app.post("/tokenize", dependencies=guarded)
        def tokenize(body: dict):
            rt.ensure_chat()
            r = rt.http.post(f"{rt.chat_url}/tokenize", json={"content": str(body.get("content", ""))})
            return JSONResponse(r.json(), status_code=r.status_code)

        @app.post("/v1/class/start", dependencies=guarded)
        def class_start():
            with self._class_lock:
                if mgr.state != "idle":
                    raise HTTPException(409, "The PC is recording a class right now.")
                if self.remote_class:
                    if time.time() - self.remote_class["last"] < REMOTE_CLASS_IDLE_S:
                        raise HTTPException(409, "Another remote class is already in progress.")
                fast = self.s.chat_model.parent / FAST_CHAT_MODEL
                if fast.exists():
                    rt.override = fast
                    rt.ensure_chat()
                mgr.whisper.start()
                rt.start_embed()
                now = time.time()
                self.remote_class = {"session": uuid.uuid4().hex, "started": now, "last": now}
                return {"session": self.remote_class["session"]}

        @app.post("/v1/class/stop", dependencies=guarded)
        def class_stop(body: dict):
            rc = self.remote_class
            if rc and body.get("session") == rc["session"]:
                self._end_remote_class()
            return {"ok": True}

        @app.post("/v1/transcribe", dependencies=guarded)
        def transcribe(body: dict):
            rc = self.remote_class
            if not rc or body.get("session") != rc["session"]:
                raise HTTPException(409, "No remote class is open with that session.")
            rc["last"] = time.time()
            wav = base64.b64decode(body.get("audio_b64", ""))
            if not wav or len(wav) > 8 * 1024 * 1024:
                raise HTTPException(400, "Audio empty or too long")
            text = mgr.whisper.transcribe_wav(wav, str(body.get("language", "auto")),
                                              str(body.get("prompt", "")))
            return {"text": text}

        return app
