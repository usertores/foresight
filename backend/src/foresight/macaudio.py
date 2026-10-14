"""The Mac's audio output (the online class) for class recordings.

macOS has no loopback device like Windows' WASAPI, so a small Swift helper (syscap.swift)
captures the system audio with ScreenCaptureKit and pipes 16 kHz mono float32 to us. It is
compiled on first use with the Command Line Tools' swiftc. SystemAudio mimics a soundcard
microphone (recorder(...).record(n)), so Capture uses it unchanged.
"""

from __future__ import annotations

import os
import select
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from .config import PROJECT_ROOT

SOURCE = Path(__file__).with_name("syscap.swift")
HELPER = PROJECT_ROOT / "runtime" / "syscap"

NO_PERMISSION = ("macOS won't let the app record the Mac's audio. Enable Foresight in System "
                 "Settings → Privacy & Security → Screen & System Audio Recording, then reopen "
                 "the app.")

_build_lock = threading.Lock()


def helper() -> Path:
    """Path to the compiled helper, (re)building it if the source is newer."""
    with _build_lock:
        if HELPER.exists() and HELPER.stat().st_mtime >= SOURCE.stat().st_mtime:
            return HELPER
        HELPER.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["xcrun", "swiftc", "-O", "-swift-version", "5", "-o", str(HELPER),
                            str(SOURCE)], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError("Couldn't build the Mac's audio capture helper "
                               f"(are Xcode's Command Line Tools installed?): {r.stderr[-500:]}")
        return HELPER


def check_permission() -> None:
    """Raise with instructions if macOS won't let us capture; asks for it the first time."""
    r = subprocess.run([str(helper()), "--check"], capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(NO_PERMISSION)


class SystemAudio:
    """Stand-in for soundcard's loopback microphone on Windows."""

    name = "Mac audio"

    def recorder(self, samplerate: int, channels: int = 1, blocksize: int | None = None):
        assert channels == 1
        return _Recorder(samplerate)


class _Recorder:
    def __init__(self, samplerate: int):
        self.sr = samplerate
        self.proc: subprocess.Popen | None = None
        self._buf = bytearray()
        self._log: deque[str] = deque(maxlen=20)
        self._ready = threading.Event()

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:
            line = line.decode("utf-8", "replace").strip()
            self._log.append(line)
            if line == "ready":
                self._ready.set()

    def _failure(self) -> str:
        code = self.proc.poll()
        if code == 2:
            return NO_PERMISSION
        return "The Mac's audio capture helper quit: " + " / ".join(self._log)

    def __enter__(self) -> "_Recorder":
        self.proc = subprocess.Popen([str(helper())], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._read_stderr, daemon=True, name="syscap-log").start()
        deadline = time.monotonic() + 15
        while not self._ready.wait(0.1):
            if self.proc.poll() is not None:
                msg = self._failure()
            elif time.monotonic() > deadline:
                msg = "The Mac's audio capture didn't start in time."
            else:
                continue
            self.__exit__()
            raise RuntimeError(msg)
        return self

    def __exit__(self, *_) -> None:
        if not self.proc:
            return
        try:
            self.proc.stdin.close()  # the helper quits when its stdin closes
            self.proc.wait(3)
        except Exception:
            self.proc.kill()
        self.proc = None

    def record(self, numframes: int) -> np.ndarray:
        """Up to numframes samples, shape (n, 1). Returns early (possibly empty) when the Mac
        is silent and nothing arrives, so the caller can pad the gap and notice a stop."""
        want = numframes * 4
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + 2 * numframes / self.sr
        while len(self._buf) < want:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                break
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                raise RuntimeError(self._failure())
            self._buf += chunk
        n = min(len(self._buf), want) // 4 * 4
        out = np.frombuffer(bytes(self._buf[:n]), dtype=np.float32)
        del self._buf[:n]
        return out.reshape(-1, 1)
