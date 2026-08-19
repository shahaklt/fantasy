"""Thin array/RNG shim so the simulator runs identically on NumPy or CUDA torch.

Only the handful of operations the engine actually needs are wrapped. The torch
path keeps every tensor on the device for the whole chunk -- the only transfer
is the finished per-player points matrix coming back.
"""
from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from ..config import Backend, detect_backend


class ArrayBackend:
    """NumPy implementation (also the reference semantics for the torch one)."""

    kind = "numpy"

    def __init__(self, seed: int | None = None, backend: Backend | None = None):
        self.backend = backend or detect_backend()
        self.rng = np.random.default_rng(seed)

    # -- construction -------------------------------------------------------
    def asarray(self, x, dtype=np.float32):
        return np.asarray(x, dtype=dtype)

    def zeros(self, shape, dtype=np.float32):
        return np.zeros(shape, dtype=dtype)

    def full(self, shape, value, dtype=np.float32):
        return np.full(shape, value, dtype=dtype)

    def to_numpy(self, x) -> np.ndarray:
        return np.asarray(x)

    # -- random -------------------------------------------------------------
    def normal(self, shape, loc=0.0, scale=1.0):
        return (self.rng.standard_normal(shape, dtype=np.float32) * scale + loc).astype(np.float32)

    def uniform(self, shape):
        return self.rng.random(shape, dtype=np.float32)

    def gamma(self, shape_param, scale=1.0):
        k = np.maximum(np.asarray(shape_param, dtype=np.float64), 0.0)
        out = np.zeros_like(k, dtype=np.float64)
        nz = k > 0
        if nz.any():
            out[nz] = self.rng.gamma(k[nz], 1.0)
        return (out * np.asarray(scale, dtype=np.float64)).astype(np.float32)

    def poisson(self, lam):
        lam = np.maximum(np.asarray(lam, dtype=np.float64), 0.0)
        return self.rng.poisson(lam).astype(np.float32)

    def binomial(self, n, p):
        n = np.maximum(np.rint(np.asarray(n, dtype=np.float64)), 0).astype(np.int64)
        p = np.clip(np.asarray(p, dtype=np.float64), 0.0, 1.0)
        return self.rng.binomial(n, p).astype(np.float32)

    # -- math ---------------------------------------------------------------
    def clip(self, x, lo, hi):
        return np.clip(x, lo, hi)

    def maximum(self, x, y):
        return np.maximum(x, y)

    def minimum(self, x, y):
        return np.minimum(x, y)

    def where(self, cond, a, b):
        return np.where(cond, a, b)

    def round(self, x):
        return np.rint(x)

    def matmul(self, a, b):
        return a @ b

    def take(self, x, idx, axis=-1):
        return np.take(x, idx, axis=axis)

    def concat(self, xs: Sequence[Any], axis=0):
        return np.concatenate(xs, axis=axis)

    def sum(self, x, axis=None):
        return np.sum(x, axis=axis)


class TorchBackend(ArrayBackend):
    """CUDA (or torch-CPU) implementation."""

    kind = "torch"

    def __init__(self, seed: int | None = None, backend: Backend | None = None):
        import torch

        self.torch = torch
        self.backend = backend or detect_backend()
        self.device = torch.device(self.backend.device or "cpu")
        self.gen = torch.Generator(device=self.device)
        if seed is not None:
            self.gen.manual_seed(int(seed))
        else:
            self.gen.seed()
        self.dtype = torch.float32

    def asarray(self, x, dtype=None):
        t = self.torch
        if isinstance(x, t.Tensor):
            return x.to(self.device, dtype=self.dtype)
        return t.as_tensor(np.asarray(x), dtype=self.dtype, device=self.device)

    def zeros(self, shape, dtype=None):
        return self.torch.zeros(shape, dtype=self.dtype, device=self.device)

    def full(self, shape, value, dtype=None):
        return self.torch.full(shape, float(value), dtype=self.dtype, device=self.device)

    def to_numpy(self, x) -> np.ndarray:
        if isinstance(x, self.torch.Tensor):
            return x.detach().to("cpu").numpy()
        return np.asarray(x)

    def normal(self, shape, loc=0.0, scale=1.0):
        z = self.torch.randn(shape, generator=self.gen, device=self.device, dtype=self.dtype)
        return z * scale + loc

    def uniform(self, shape):
        return self.torch.rand(shape, generator=self.gen, device=self.device, dtype=self.dtype)

    def gamma(self, shape_param, scale=1.0):
        t = self.torch
        k = self.asarray(shape_param).clamp(min=0.0)
        # torch's sampler is undefined at concentration 0; sample with a floor and
        # mask the result, which is exactly Gamma(0) = 0.
        safe = k.clamp(min=1e-6)
        g = t._standard_gamma(safe, self.gen) if hasattr(t, "_standard_gamma") else \
            t.distributions.Gamma(safe, t.ones_like(safe)).sample()
        g = t.nan_to_num(g, nan=0.0, posinf=0.0)
        return g * (k > 0).to(self.dtype) * self.asarray(scale)

    def poisson(self, lam):
        lam = self.asarray(lam).clamp(min=0.0)
        return self.torch.poisson(lam, generator=self.gen)

    def binomial(self, n, p):
        t = self.torch
        n = self.asarray(n).round().clamp(min=0.0)
        p = self.asarray(p).clamp(0.0, 1.0)
        return t.binomial(n, p, generator=self.gen)

    def clip(self, x, lo, hi):
        return self.asarray(x).clamp(float(lo), float(hi))

    def maximum(self, x, y):
        return self.torch.maximum(self.asarray(x), self.asarray(y))

    def minimum(self, x, y):
        return self.torch.minimum(self.asarray(x), self.asarray(y))

    def where(self, cond, a, b):
        return self.torch.where(cond, self.asarray(a), self.asarray(b))

    def round(self, x):
        return self.torch.round(self.asarray(x))

    def matmul(self, a, b):
        return self.asarray(a) @ self.asarray(b)

    def take(self, x, idx, axis=-1):
        idx_t = self.torch.as_tensor(np.asarray(idx), dtype=self.torch.long, device=self.device)
        return self.torch.index_select(self.asarray(x), axis if axis >= 0 else x.dim() + axis, idx_t)

    def concat(self, xs, axis=0):
        return self.torch.cat([self.asarray(x) for x in xs], dim=axis)

    def sum(self, x, axis=None):
        return self.torch.sum(self.asarray(x)) if axis is None else self.torch.sum(self.asarray(x), dim=axis)


def make_backend(seed: int | None = None, prefer: str | None = None) -> ArrayBackend:
    """Pick the array backend, honouring ``GRIDIRON_DEVICE`` / ``prefer``."""
    b = detect_backend()
    if prefer == "numpy" or b.name == "numpy":
        return ArrayBackend(seed=seed, backend=b)
    try:
        return TorchBackend(seed=seed, backend=b)
    except Exception:  # noqa: BLE001 - torch present but unusable
        return ArrayBackend(seed=seed, backend=b)
