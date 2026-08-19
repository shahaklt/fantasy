"""Array/RNG shim so the simulator runs identically on NumPy, CuPy or torch.

The base class is written against ``self.np`` -- a module reference, not a
hard-coded import -- because CuPy mirrors the NumPy API closely enough that
swapping the module is genuinely most of the work. :class:`CuPyBackend` is
therefore only a constructor and three conversion overrides.

What a blanket ``import cupy as np`` across the project would *not* survive:

* ``scipy.stats`` / ``scipy.optimize`` (the analytic game model, devigging,
  blend-weight fitting) reject device arrays outright;
* Polars cannot build a frame from device memory, and every projection table
  and API response is Polars-backed;
* JSON serialisation and ``float()`` need host memory, so each one becomes a
  synchronising copy;
* most of the quant layer works on a few hundred elements, where a kernel
  launch plus two transfers costs more than the arithmetic saves.

So the device backend owns the Monte Carlo hot loop -- ``(n_sims, n_players)``
arrays, tens of millions of elements per pass -- and NumPy keeps everything
else. That split is what the abstraction is for.
"""
from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from ..config import Backend, detect_backend


class ArrayBackend:
    """NumPy implementation, and the reference semantics for the others.

    Every method routes through ``self.np`` so a CuPy subclass inherits the
    whole implementation unchanged.
    """

    kind = "numpy"
    #: Array module. CuPy overrides this; everything else follows.
    np = np

    def __init__(self, seed: int | None = None, backend: Backend | None = None):
        self.backend = backend or detect_backend()
        self.rng = np.random.default_rng(seed)
        self.float = np.float32

    @property
    def device_label(self) -> str:
        return "cpu"

    # -- construction -------------------------------------------------------
    def asarray(self, x, dtype=None):
        return self.np.asarray(x, dtype=dtype or self.float)

    def zeros(self, shape, dtype=None):
        return self.np.zeros(shape, dtype=dtype or self.float)

    def full(self, shape, value, dtype=None):
        return self.np.full(shape, value, dtype=dtype or self.float)

    def to_numpy(self, x) -> np.ndarray:
        return np.asarray(x)

    # -- random -------------------------------------------------------------
    def normal(self, shape, loc=0.0, scale=1.0):
        z = self.rng.standard_normal(shape, dtype=self.float)
        return z * scale + loc

    def uniform(self, shape):
        return self.rng.random(shape, dtype=self.float)

    def gamma(self, shape_param, scale=1.0):
        """Gamma draws, safe at shape 0 (which is a point mass at zero).

        Clamping and masking rather than boolean-indexing the non-zero entries
        keeps this a single fixed-size kernel, which matters on a GPU where a
        data-dependent gather would force a host synchronisation.
        """
        k = self.asarray(shape_param, dtype=self.np.float64)
        k = self.np.maximum(k, 0.0)
        draws = self.rng.standard_gamma(self.np.maximum(k, 1e-9))
        return (draws * (k > 0) * self.asarray(scale, dtype=self.np.float64)).astype(self.float)

    def poisson(self, lam):
        lam = self.np.maximum(self.asarray(lam, dtype=self.np.float64), 0.0)
        return self.rng.poisson(lam).astype(self.float)

    def binomial(self, n, p):
        n = self.np.maximum(self.np.rint(self.asarray(n, dtype=self.np.float64)), 0).astype(
            self.np.int64)
        p = self.np.clip(self.asarray(p, dtype=self.np.float64), 0.0, 1.0)
        return self.rng.binomial(n, p).astype(self.float)

    # -- math ---------------------------------------------------------------
    def clip(self, x, lo, hi):
        return self.np.clip(x, lo, hi)

    def maximum(self, x, y):
        return self.np.maximum(x, y)

    def minimum(self, x, y):
        return self.np.minimum(x, y)

    def where(self, cond, a, b):
        return self.np.where(cond, a, b)

    def round(self, x):
        return self.np.rint(x)

    def exp(self, x):
        return self.np.exp(x)

    def matmul(self, a, b):
        return a @ b

    def take(self, x, idx, axis=-1):
        return self.np.take(x, self.index(idx), axis=axis)

    def index(self, idx):
        """Index arrays must live on the same device as the data they select."""
        return np.asarray(idx)

    def scatter_columns(self, dest, idx, values):
        """``dest[:, idx] = values``, returned as a new array."""
        out = dest.copy()
        out[:, self.index(idx)] = values
        return out

    def concat(self, xs: Sequence[Any], axis=0):
        return self.np.concatenate(xs, axis=axis)

    def sum(self, x, axis=None):
        return self.np.sum(x, axis=axis)


class CuPyBackend(ArrayBackend):
    """CUDA backend via CuPy.

    Preferred over torch for this workload: the API is NumPy's, so the shared
    implementation above runs unchanged, and the install is a fraction of the
    size with no autograd machinery in the way.
    """

    kind = "cupy"

    def __init__(self, seed: int | None = None, backend: Backend | None = None):
        import cupy

        self.np = cupy
        self.backend = backend or detect_backend()
        self.rng = cupy.random.default_rng(seed)
        self.float = cupy.float32
        # Fail here rather than deep inside a simulation if the driver is absent.
        cupy.cuda.runtime.getDeviceCount()

    @property
    def device_label(self) -> str:
        return f"cuda:{self.np.cuda.runtime.getDevice()}"

    def to_numpy(self, x) -> np.ndarray:
        return self.np.asnumpy(x) if isinstance(x, self.np.ndarray) else np.asarray(x)

    def index(self, idx):
        # CuPy refuses host index arrays for fancy indexing on device arrays.
        return self.np.asarray(np.asarray(idx))

    def binomial(self, n, p):
        # CuPy's binomial requires cupy arrays (or scalars) for both parameters.
        n = self.np.maximum(self.np.rint(self.asarray(n, dtype=self.np.float64)), 0).astype(
            self.np.int64)
        p = self.np.clip(self.asarray(p, dtype=self.np.float64), 0.0, 1.0)
        return self.rng.binomial(n, p).astype(self.float)

    def synchronize(self) -> None:
        self.np.cuda.Stream.null.synchronize()


class TorchBackend(ArrayBackend):
    """CUDA (or torch-CPU) backend, kept as a fallback when CuPy is absent."""

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
        self.float = torch.float32

    @property
    def device_label(self) -> str:
        return str(self.device)

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
        safe = k.clamp(min=1e-6)
        g = t._standard_gamma(safe, self.gen) if hasattr(t, "_standard_gamma") else \
            t.distributions.Gamma(safe, t.ones_like(safe)).sample()
        g = t.nan_to_num(g, nan=0.0, posinf=0.0)
        return g * (k > 0).to(self.dtype) * self.asarray(scale)

    def poisson(self, lam):
        return self.torch.poisson(self.asarray(lam).clamp(min=0.0), generator=self.gen)

    def binomial(self, n, p):
        n = self.asarray(n).round().clamp(min=0.0)
        p = self.asarray(p).clamp(0.0, 1.0)
        return self.torch.binomial(n, p, generator=self.gen)

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

    def exp(self, x):
        return self.torch.exp(self.asarray(x))

    def matmul(self, a, b):
        return self.asarray(a) @ self.asarray(b)

    def index(self, idx):
        return self.torch.as_tensor(np.asarray(idx), dtype=self.torch.long, device=self.device)

    def take(self, x, idx, axis=-1):
        dim = axis if axis >= 0 else self.asarray(x).dim() + axis
        return self.torch.index_select(self.asarray(x), dim, self.index(idx))

    def scatter_columns(self, dest, idx, values):
        index = self.index(idx)[None, :].expand(values.shape[0], -1)
        return dest.scatter(1, index, self.asarray(values))

    def concat(self, xs, axis=0):
        return self.torch.cat([self.asarray(x) for x in xs], dim=axis)

    def sum(self, x, axis=None):
        x = self.asarray(x)
        return self.torch.sum(x) if axis is None else self.torch.sum(x, dim=axis)

    def synchronize(self) -> None:
        if self.device.type == "cuda":
            self.torch.cuda.synchronize()


#: Device backends in preference order. CuPy first: same API as NumPy, so the
#: shared implementation runs unchanged, and it installs far smaller than torch.
GPU_BACKENDS = (("cupy", CuPyBackend), ("torch", TorchBackend))


def make_backend(seed: int | None = None, prefer: str | None = None) -> ArrayBackend:
    """Pick the array backend, honouring ``GRIDIRON_DEVICE`` / ``prefer``.

    Falls back rather than failing: a missing or broken GPU library drops to the
    next option and finally to NumPy, so a simulation always runs.
    """
    b = detect_backend()
    if prefer == "numpy" or b.name == "numpy":
        return ArrayBackend(seed=seed, backend=b)

    candidates = GPU_BACKENDS
    if prefer in ("cupy", "torch"):
        candidates = tuple(c for c in GPU_BACKENDS if c[0] == prefer)
    for _, cls in candidates:
        try:
            return cls(seed=seed, backend=b)
        except Exception:  # noqa: BLE001 - library missing or device unusable
            continue
    return ArrayBackend(seed=seed, backend=b)
