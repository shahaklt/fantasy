"""Backend contract and parity tests.

The CuPy path cannot be executed without an NVIDIA device, so it is verified
two ways: a contract test that runs anywhere and proves the subclass covers the
whole interface, and parity tests that run against every backend actually
importable on this machine.
"""
from __future__ import annotations

import inspect

import numpy as np
import pytest

from gridiron.sim.backend import (ArrayBackend, CuPyBackend, TorchBackend,
                                  make_backend)

#: Everything the engine calls on a backend.
INTERFACE = ("asarray", "zeros", "full", "to_numpy", "normal", "uniform", "gamma",
             "poisson", "binomial", "clip", "maximum", "minimum", "where", "round",
             "exp", "matmul", "take", "index", "scatter_columns", "concat", "sum")


def available_backends():
    """Every backend importable here; CuPy and torch are skipped if absent."""
    out = [("numpy", ArrayBackend)]
    for name, cls in (("cupy", CuPyBackend), ("torch", TorchBackend)):
        try:
            cls(seed=0)
        except Exception:  # noqa: BLE001
            continue
        out.append((name, cls))
    return out


BACKENDS = available_backends()
IDS = [n for n, _ in BACKENDS]


class TestContract:
    """Runs without any GPU: the subclasses must cover the whole interface."""

    @pytest.mark.parametrize("cls", [ArrayBackend, CuPyBackend, TorchBackend])
    def test_every_method_is_present(self, cls):
        missing = [m for m in INTERFACE if not callable(getattr(cls, m, None))]
        assert missing == [], f"{cls.__name__} is missing {missing}"

    @pytest.mark.parametrize("cls", [CuPyBackend, TorchBackend])
    def test_signatures_match_the_base_class(self, cls):
        """An override must accept the same arguments the engine passes."""
        for name in INTERFACE:
            base = getattr(ArrayBackend, name)
            override = getattr(cls, name)
            if override is base:
                continue        # inherited unchanged
            assert (list(inspect.signature(override).parameters)
                    == list(inspect.signature(base).parameters)), f"{cls.__name__}.{name}"

    def test_cupy_inherits_most_of_the_implementation(self):
        """CuPy should override conversions only -- the maths is shared.

        If this starts failing, the NumPy-compatible path has drifted and the
        two backends can diverge silently.
        """
        overridden = {m for m in INTERFACE
                      if getattr(CuPyBackend, m) is not getattr(ArrayBackend, m)}
        assert overridden <= {"to_numpy", "index", "binomial"}

    def test_numpy_is_requested_explicitly(self):
        assert make_backend(seed=1, prefer="numpy").kind == "numpy"

    def test_unavailable_backend_falls_back_rather_than_raising(self):
        # A machine without CuPy must still get a working backend.
        assert make_backend(seed=1, prefer="cupy").kind in ("cupy", "numpy", "torch")


@pytest.mark.parametrize("name,cls", BACKENDS, ids=IDS)
class TestParity:
    """Statistical behaviour must match across whichever backends exist here."""

    def test_gamma_is_zero_at_shape_zero(self, name, cls):
        xp = cls(seed=1)
        out = xp.to_numpy(xp.gamma(np.array([0.0, 2.0, 0.0]), np.array([1.0, 1.0, 5.0])))
        assert out[0] == 0.0 and out[2] == 0.0
        assert out[1] > 0.0

    def test_gamma_matches_its_analytic_mean(self, name, cls):
        xp = cls(seed=2)
        k, scale = 2.084, 4.7
        draws = xp.to_numpy(xp.gamma(np.full(120_000, k), scale))
        assert draws.mean() == pytest.approx(k * scale, rel=0.02)
        assert draws.std() == pytest.approx(np.sqrt(k) * scale, rel=0.05)

    def test_binomial_respects_its_bounds(self, name, cls):
        xp = cls(seed=3)
        out = xp.to_numpy(xp.binomial(np.full(4000, 10.0), np.full(4000, 0.5)))
        assert out.min() >= 0 and out.max() <= 10
        assert out.mean() == pytest.approx(5.0, abs=0.15)

    def test_binomial_broadcasts_per_element_parameters(self, name, cls):
        """The engine relies on array n *and* array p, not scalars."""
        xp = cls(seed=4)
        n = np.tile(np.array([0.0, 5.0, 20.0]), 2000)
        p = np.tile(np.array([0.5, 1.0, 0.0]), 2000)
        out = xp.to_numpy(xp.binomial(n, p))
        assert (out[0::3] == 0).all()      # n = 0
        assert (out[1::3] == 5).all()      # p = 1
        assert (out[2::3] == 0).all()      # p = 0

    def test_poisson_matches_its_mean(self, name, cls):
        xp = cls(seed=5)
        out = xp.to_numpy(xp.poisson(np.full(60_000, 2.4)))
        assert out.mean() == pytest.approx(2.4, rel=0.03)
        assert out.min() >= 0

    def test_normal_matches_its_moments(self, name, cls):
        xp = cls(seed=6)
        out = xp.to_numpy(xp.normal((80_000,), 3.0, 2.0))
        assert out.mean() == pytest.approx(3.0, abs=0.05)
        assert out.std() == pytest.approx(2.0, rel=0.03)

    def test_take_selects_columns(self, name, cls):
        xp = cls(seed=7)
        src = xp.asarray(np.arange(12.0).reshape(3, 4))
        assert xp.to_numpy(xp.take(src, [3, 0], axis=1)).tolist() == [
            [3.0, 0.0], [7.0, 4.0], [11.0, 8.0]]

    def test_scatter_writes_the_named_columns_only(self, name, cls):
        xp = cls(seed=8)
        dest = xp.zeros((2, 4))
        out = xp.to_numpy(xp.scatter_columns(dest, np.array([1, 3]), xp.full((2, 2), 9.0)))
        assert out.tolist() == [[0.0, 9.0, 0.0, 9.0], [0.0, 9.0, 0.0, 9.0]]
        # The source must not be mutated -- the engine reuses it.
        assert xp.to_numpy(dest).sum() == 0.0

    def test_matmul_aggregates_players_into_teams(self, name, cls):
        """The (S, P) @ (P, T) product is how per-team totals are formed."""
        xp = cls(seed=9)
        values = xp.asarray(np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]))
        onehot = xp.asarray(np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]))
        assert xp.to_numpy(xp.matmul(values, onehot)).tolist() == [[3.0, 3.0], [9.0, 6.0]]

    def test_exp_runs_on_the_backend(self, name, cls):
        xp = cls(seed=10)
        out = xp.to_numpy(xp.exp(xp.asarray(np.array([0.0, 1.0]))))
        assert out[0] == pytest.approx(1.0)
        assert out[1] == pytest.approx(np.e, rel=1e-5)

    def test_to_numpy_returns_host_memory(self, name, cls):
        xp = cls(seed=11)
        out = xp.to_numpy(xp.zeros((3, 3)))
        assert isinstance(out, np.ndarray)
        assert out.shape == (3, 3)
