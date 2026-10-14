"""Settings. Defaults live here; overrides come from <project>/config.json."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .profile import Profile

PROJECT_ROOT = Path(__file__).resolve().parents[3]
# FORESIGHT_CONFIG lets a second instance (tests, another setup) use its own settings.
CONFIG_FILE = Path(os.environ.get("FORESIGHT_CONFIG") or PROJECT_ROOT / "config.json")


@dataclass
class Settings:
    # Folder that gets indexed (PDFs + Markdown). The app asks for it on first run.
    corpus_dir: Path = PROJECT_ROOT / "library"
    # Where the vector index and manifest are stored.
    data_dir: Path = PROJECT_ROOT / "data"
    # Study profile (JSON, relative to the project root), e.g. "profiles/cfa-level-1.json".
    profile: str = ""

    # Both models run on llama.cpp (Vulkan build) as two local servers.
    llama_server: Path = PROJECT_ROOT / "runtime" / "llama.cpp" / "llama-server.exe"
    # "auto" picks the NVIDIA GPU if llama.cpp sees one, otherwise CPU.
    device: str = "auto"

    # Retrieval: EmbeddingGemma 2.
    embed_model: Path = PROJECT_ROOT / "models" / "embeddinggemma-2-Q8_0.gguf"
    embed_port: int = 8081
    embed_dims: int = 768

    # Generation: Gemma 4.
    chat_model: Path = PROJECT_ROOT / "models" / "gemma-4-e2b-it-qat.gguf"
    chat_port: int = 8082
    chat_ctx: int = 8192

    # Meetings: Whisper large-v3-turbo on whisper.cpp (CUDA 12 build).
    whisper_server: Path = PROJECT_ROOT / "runtime" / "whisper.cpp" / "Release" / "whisper-server.exe"
    whisper_model: Path = PROJECT_ROOT / "models" / "ggml-large-v3-turbo-q5_0.bin"
    whisper_port: int = 8083

    # HTTP API used by the desktop app.
    api_port: int = 8765

    # Server mode: lend this PC's models to the user's other computers (off by default).
    gateway_enabled: bool = False
    gateway_port: int = 8770
    # "tailscale" binds to this PC's Tailscale address only; or an explicit IP.
    gateway_host: str = "tailscale"
    gateway_token: str = ""

    # Client mode (e.g. a Mac): use another PC's gateway instead of local models.
    remote_url: str = ""
    remote_token: str = ""

    # Chunking.
    chunk_words: int = 220
    chunk_overlap: int = 40

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        if CONFIG_FILE.exists():
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for f in fields(cls):
                if f.name in raw:
                    default = getattr(s, f.name)
                    value = raw[f.name]
                    setattr(s, f.name, Path(value) if isinstance(default, Path) else type(default)(value))
        s.data_dir.mkdir(parents=True, exist_ok=True)
        return s

    def load_profile(self) -> Profile:
        if not self.profile:
            return Profile()
        path = Path(self.profile)
        return Profile.load(path if path.is_absolute() else PROJECT_ROOT / path)

    def save(self) -> None:
        data = {k: str(v) if isinstance(v, Path) else v for k, v in asdict(self).items()}
        CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
