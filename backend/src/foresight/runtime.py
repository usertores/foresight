"""Start/stop the two llama.cpp servers (embeddings + chat) and talk to them."""

from __future__ import annotations

import atexit
import base64
import contextlib
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import numpy as np

from .config import Settings

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

FAST_CHAT_MODEL = "gemma-4-e2b-it-qat.gguf"  # live answers in class, drafts of long notes

QUERY_PREFIX = "task: search result | query: "
DOC_PREFIX = "title: {title} | text: "
MAX_EMBED_WORDS = 1200  # keeps every input inside one 4096-token physical batch


def pick_device(server: Path) -> str:
    """Return the llama.cpp device name of the NVIDIA GPU, or 'none' for CPU."""
    try:
        out = subprocess.run(
            [str(server), "--list-devices"], capture_output=True, text=True, timeout=60,
            creationflags=CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        return "none"
    for line in out.splitlines():
        m = re.match(r"\s*(\w+\d+):\s*(.*)", line)
        if m and "nvidia" in m.group(2).lower():
            return m.group(1)
    return "none"


class LlamaServer:
    def __init__(self, name: str, exe: Path, model: Path, port: int, args: list[str],
                 gpu_args: list[str]):
        self.name, self.exe, self.model, self.port, self.args = name, exe, model, port, args
        self.gpu_args = gpu_args
        self.url = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen | None = None
        self.log_path: Path | None = None

    def healthy(self) -> bool:
        try:
            return httpx.get(f"{self.url}/health", timeout=1).json().get("status") == "ok"
        except Exception:
            return False

    def start(self, device: str, log_dir: Path, timeout: float = 180) -> None:
        if self.healthy():  # left over from a previous run: reuse it
            return
        self.log_path = log_dir / f"{self.name}.log"
        cmd = [str(self.exe), "--model", str(self.model), "--host", "127.0.0.1",
               "--port", str(self.port), "--device", device, *self.args]
        if device != "none":
            cmd += self.gpu_args
        log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=CREATE_NO_WINDOW)
        atexit.register(self.stop)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.name} server exited; see {self.log_path}")
            if self.healthy():
                return
            time.sleep(0.5)
        raise TimeoutError(f"{self.name} server did not become ready; see {self.log_path}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


class Runtime:
    """Owns both servers. Embeddings start eagerly; chat starts on first use."""

    remote = False

    def __init__(self, s: Settings):
        self.s = s
        self.device = s.device if s.device != "auto" else pick_device(s.llama_server)
        log_dir = s.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir = log_dir
        self.embed_srv = LlamaServer(
            "embed", s.llama_server, s.embed_model, s.embed_port,
            ["--alias", "embeddinggemma2", "--embeddings", "--pooling", "mean",
             "--ctx-size", "8192", "--batch-size", "4096", "--ubatch-size", "4096",
             "--parallel", "2"],
            gpu_args=["-ngl", "99"],  # ~0.3 GB: always fully on the GPU
        )
        self.chat_srv = LlamaServer(
            "chat", s.llama_server, s.chat_model, s.chat_port,
            ["--alias", "gemma4", "--jinja", "--ctx-size", str(s.chat_ctx), "--parallel", "1"],
            # Let llama.cpp fit as many layers as free VRAM allows (E4B spills to RAM).
            gpu_args=["--fit-target", "256"],
        )
        self._chat_lock = threading.Lock()
        self._embed_lock = threading.Lock()
        self.override: Path | None = None  # forces a model (the fast one during a class)
        self._fast_users = 0  # blocks inside prefer_fast() (e.g. drafts of long notes)
        self.http = httpx.Client(timeout=300)

    # ------------------------------------------------------------ embeddings

    def start_embed(self) -> None:
        # Locked so that concurrent callers (search + indexer) never launch two servers.
        with self._embed_lock:
            self.embed_srv.start(self.device, self.log_dir)

    def _embed(self, texts: list[str]) -> np.ndarray:
        url = f"{self.embed_srv.url}/v1/embeddings"
        try:
            r = self.http.post(url, json={"input": texts})
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
            # The embeddings server died (crash, or something closed it): bring it back and
            # retry once, instead of failing every search until the app is restarted.
            self.start_embed()
            r = self.http.post(url, json={"input": texts})
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        v = np.asarray([d["embedding"] for d in data], dtype=np.float32)[:, : self.s.embed_dims]
        return v / np.linalg.norm(v, axis=1, keepdims=True).clip(min=1e-12)

    def embed_documents(self, items: list[tuple[str, str]]) -> np.ndarray:
        """items: (title, text) pairs."""
        texts = [DOC_PREFIX.format(title=t or "none") + " ".join(x.split()[:MAX_EMBED_WORDS])
                 for t, x in items]
        return self._embed(texts)

    def embed_query(self, q: str) -> np.ndarray:
        return self._embed([QUERY_PREFIX + q])[0]

    # ------------------------------------------------------------ chat

    def ensure_chat(self) -> None:
        """Make sure the wanted Gemma model is loaded, swapping models if needed.

        The wanted model is `override` (the fast one while a class is recorded) or else the
        user's choice. Check and swap happen under one lock, so a request never lands on a
        server that is being replaced.
        """
        with self._chat_lock:
            fast = self.s.chat_model.parent / FAST_CHAT_MODEL
            wanted = fast if self._fast_users and fast.exists() else self.s.chat_model
            target = self.override or wanted
            if self.chat_srv.model == target and self.chat_srv.healthy():
                return
            self.chat_srv.stop()
            self.chat_srv.model = target
            self.chat_srv.start(self.device, self.log_dir)
            self._warmup()

    @contextlib.contextmanager
    def prefer_fast(self):
        """Use the fast Gemma model for the calls made inside this block.

        For bulk work where speed matters more than polish (summarising each part of a long
        class); the user's choice is loaded again for the next call outside the block.
        """
        with self._chat_lock:
            self._fast_users += 1
        try:
            yield
        finally:
            with self._chat_lock:
                self._fast_users -= 1

    @property
    def chat_url(self) -> str:
        """Base URL of the OpenAI-compatible chat server (POST {chat_url}/v1/chat/completions)."""
        return self.chat_srv.url

    def chat_loaded(self) -> str:
        """Stem of the Gemma model currently loaded (or about to be)."""
        return self.chat_srv.model.stem

    def chat_ready(self) -> bool:
        return self.chat_srv.healthy()

    def count_tokens(self, text: str) -> int:
        self.ensure_chat()
        r = self.http.post(f"{self.chat_url}/tokenize", json={"content": text})
        r.raise_for_status()
        return len(r.json()["tokens"])

    def _warmup(self) -> None:
        # Vulkan compiles shaders lazily per batch shape; one prompt longer than a batch
        # (512) covers the shapes real requests use. The driver caches them on disk.
        filler = "warm up " * 300
        try:
            self.http.post(f"{self.chat_srv.url}/v1/chat/completions", json={
                "messages": [{"role": "user", "content": filler}], "max_tokens": 1,
                "chat_template_kwargs": {"enable_thinking": False}, "cache_prompt": False,
            })
        except Exception:
            pass

    def status(self) -> dict:
        return {
            "device": self.device,
            "embed": self.embed_srv.healthy(),
            "chat": self.chat_srv.healthy(),
            "chat_model": self.chat_srv.model.stem,  # what is loaded right now
            "chat_selected": self.s.chat_model.name,  # what the user picked
        }

    def stop(self) -> None:
        self.embed_srv.stop()
        self.chat_srv.stop()


class RemoteError(RuntimeError):
    """The PC can't be reached or rejected the password; the message says what to do."""


def normalize_remote_url(url: str, default_port: int) -> str:
    """'my-pc' / 'my-pc:8770' / 'http://my-pc:8770/' -> 'http://my-pc:8770'."""
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    if parts.hostname and parts.port is None:
        url = f"{parts.scheme}://{parts.hostname}:{default_port}{parts.path}"
    return url


class _PCTransport(httpx.HTTPTransport):
    """Turns network failures and a rejected password into messages the user can act on."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        try:
            response = super().handle_request(request)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise RemoteError(f"Can't connect to the PC ({host}). Turn it on, open Foresight "
                              "and enable Server; check that Tailscale is connected on both "
                              "computers.") from e
        if response.status_code == 401:
            response.close()
            raise RemoteError("The PC doesn't accept the password. Copy it from the PC's Server "
                              "panel and connect again.")
        return response


class RemoteRuntime:
    """Same interface as Runtime, but the models run on another PC behind its gateway.

    Used on machines that can't run the models themselves (e.g. an Intel Mac). Indexing,
    search and the UI stay local; only text to embed, prompts and class audio travel to the PC.
    """

    remote = True

    def __init__(self, s: Settings):
        self.s = s
        self.log_dir = s.data_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.device = "remote"
        self.override = None  # the PC picks the model; a class asks it for the fast one
        self.connect(s.remote_url, s.remote_token)

    @property
    def configured(self) -> bool:
        return bool(self.base)

    def connect(self, url: str, token: str) -> None:
        """Point at a (new) PC; requests already in flight finish on the old client."""
        self.base = url.rstrip("/")
        self.http = make_remote_client(token)
        self._health: dict = {}
        self._health_at = 0.0

    def health(self, max_age: float = 5) -> dict:
        if not self.configured:
            return {"ok": False, "error": "This computer isn't connected to a PC yet."}
        if time.time() - self._health_at > max_age:
            try:
                r = self.http.get(f"{self.base}/v1/health", timeout=4)
                r.raise_for_status()
                self._health = r.json()
            except RemoteError as e:
                self._health = {"ok": False, "error": str(e)}
            except Exception as e:
                self._health = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self._health_at = time.time()
        return self._health

    # ------------------------------------------------------------ embeddings

    def start_embed(self) -> None:
        h = self.health(max_age=0)
        if not h.get("ok"):
            raise RemoteError(h.get("error") or f"Can't connect to the PC ({self.base})")

    def _embed(self, texts: list[str]) -> np.ndarray:
        r = self.http.post(f"{self.base}/v1/embed", json={"input": texts})
        r.raise_for_status()
        d = r.json()
        v = np.frombuffer(base64.b64decode(d["b64"]), dtype=np.float32).reshape(d["count"], d["dims"])
        return v[:, : self.s.embed_dims].copy()

    def embed_documents(self, items: list[tuple[str, str]]) -> np.ndarray:
        texts = [DOC_PREFIX.format(title=t or "none") + " ".join(x.split()[:MAX_EMBED_WORDS])
                 for t, x in items]
        return self._embed(texts)

    def embed_query(self, q: str) -> np.ndarray:
        return self._embed([QUERY_PREFIX + q])[0]

    # ------------------------------------------------------------ chat

    @property
    def chat_url(self) -> str:
        return self.base  # the gateway proxies /v1/chat/completions and /tokenize

    def ensure_chat(self) -> None:
        pass  # the PC loads the right model before answering

    def prefer_fast(self):
        return contextlib.nullcontext()  # the PC picks the model

    def chat_loaded(self) -> str:
        return self.health().get("chat_model", "")

    def chat_ready(self) -> bool:
        return bool(self.health().get("ok"))

    def ocr_images(self, pngs: list[bytes]) -> list[list[str]]:
        """Windows OCR on the PC for scanned pages rendered here (much faster than Vision)."""
        r = self.http.post(f"{self.base}/v1/ocr", timeout=120, json={
            "images": [base64.b64encode(png).decode("ascii") for png in pngs]})
        r.raise_for_status()
        return r.json()["lines"]

    def count_tokens(self, text: str) -> int:
        r = self.http.post(f"{self.base}/tokenize", json={"content": text})
        r.raise_for_status()
        return len(r.json()["tokens"])

    def status(self) -> dict:
        h = self.health()
        return {
            "device": "remote",
            "remote": self.base,
            "embed": bool(h.get("ok")),
            "chat": bool(h.get("ok")),
            "chat_model": h.get("chat_model", ""),
            "chat_selected": h.get("chat_selected", ""),
            "error": h.get("error", ""),
        }

    def stop(self) -> None:
        pass


def make_remote_client(token: str) -> httpx.Client:
    # Long reads (notes of a 1 h class) but a short connect: a PC that is off fails fast.
    return httpx.Client(timeout=httpx.Timeout(300, connect=6), transport=_PCTransport(),
                        headers={"Authorization": f"Bearer {token}"})


def make_runtime(s: Settings) -> Runtime | RemoteRuntime:
    # macOS runs as a client: it always uses a PC's models, once it has been told where.
    if s.remote_url or sys.platform == "darwin":
        return RemoteRuntime(s)
    return Runtime(s)
