"""Global configuration: paths, compute backend selection, tunables.

The engine is designed to saturate a strong desktop (e.g. i7-12700K + RTX 3060 Ti):
* CPU path  -> numpy with multi-threaded BLAS, work split across processes.
* GPU path  -> torch CUDA tensors, simulations chunked to fit 8 GB of VRAM.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------
PKG_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PKG_ROOT.parent.parent

DATA_DIR = Path(os.environ.get("GRIDIRON_DATA_DIR", REPO_ROOT / "data"))
CACHE_DIR = DATA_DIR / "cache"
ARTIFACT_DIR = DATA_DIR / "artifacts"
USER_DIR = DATA_DIR / "user"

for _p in (DATA_DIR, CACHE_DIR, ARTIFACT_DIR, USER_DIR):
    _p.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------------------
# Season constants
# --------------------------------------------------------------------------------------
#: First season with reliable nflverse participation / advanced columns.
MIN_TRAIN_SEASON = 2016
#: Regular season length in the modern (17 game / 18 week) format.
REGULAR_SEASON_WEEKS = 18

FANTASY_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DST")
SKILL_POSITIONS = ("QB", "RB", "WR", "TE")


def current_season(today=None) -> int:
    """NFL season year. A season labelled `Y` spans Sep `Y` .. Feb `Y+1`."""
    import datetime as _dt

    today = today or _dt.date.today()
    return today.year if today.month >= 3 else today.year - 1


# --------------------------------------------------------------------------------------
# Compute backend
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Backend:
    """Describes the array backend used by the Monte Carlo engine."""

    name: str  # "cuda" | "cpu-torch" | "numpy"
    device: str | None = None
    device_name: str = ""
    total_memory_gb: float = 0.0
    cpu_count: int = field(default_factory=lambda: os.cpu_count() or 4)

    @property
    def is_gpu(self) -> bool:
        return self.name == "cuda"

    def describe(self) -> str:
        if self.is_gpu:
            return f"CUDA · {self.device_name} · {self.total_memory_gb:.1f} GB VRAM"
        if self.name == "cpu-torch":
            return f"Torch CPU · {self.cpu_count} logical cores"
        return f"NumPy · {self.cpu_count} logical cores"


@lru_cache(maxsize=1)
def detect_backend() -> Backend:
    """Pick the fastest available backend, honouring ``GRIDIRON_DEVICE``."""
    forced = os.environ.get("GRIDIRON_DEVICE", "auto").lower()
    if forced == "numpy":
        return Backend(name="numpy")
    try:
        import torch  # noqa: F401
    except Exception:
        return Backend(name="numpy")

    import torch

    if forced in ("cuda", "auto") and torch.cuda.is_available():
        idx = torch.cuda.current_device()
        props = torch.cuda.get_device_properties(idx)
        return Backend(
            name="cuda",
            device=f"cuda:{idx}",
            device_name=props.name,
            total_memory_gb=props.total_memory / 1024**3,
        )
    if forced == "cuda":
        # Explicitly requested but unavailable -> be loud rather than silently slow.
        raise RuntimeError("GRIDIRON_DEVICE=cuda but no CUDA device is visible to torch")
    return Backend(name="cpu-torch", device="cpu")


def sim_chunk_size(n_entities: int, backend: Backend | None = None) -> int:
    """How many simulations to run per chunk so intermediate arrays stay in memory.

    The engine keeps roughly ``SLOTS`` float32 arrays of shape (chunk, n_entities)
    alive at once; we target ~35% of VRAM (or ~2 GB of RAM on CPU) for headroom.
    """
    backend = backend or detect_backend()
    SLOTS = 24
    bytes_per_sim_row = max(n_entities, 1) * 4 * SLOTS
    budget = (backend.total_memory_gb * 0.35 * 1024**3) if backend.is_gpu else 2 * 1024**3
    chunk = int(budget // max(bytes_per_sim_row, 1))
    return int(max(256, min(chunk, 200_000)))
