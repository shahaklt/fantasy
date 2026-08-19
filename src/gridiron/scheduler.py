"""Twice-daily refresh scheduler (00:00 and 12:00 local by default).

A small dependency-free scheduler running on a daemon thread. It computes the
next run time from wall-clock local time, so a machine that sleeps through a
scheduled slot simply runs the job late rather than skipping it -- and a job
that is already running is never started twice.

If you would rather the OS drove it, the same work is available as
``python -m gridiron.cli refresh``; see the README for cron and Task Scheduler
snippets.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import ARTIFACT_DIR

log = logging.getLogger(__name__)

HISTORY_PATH = ARTIFACT_DIR / "scheduler_history.json"
DEFAULT_TIMES = ("00:00", "12:00")
MAX_HISTORY = 60


@dataclass
class JobRun:
    job: str
    started: str
    finished: str
    ok: bool
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _parse_times(times) -> list[tuple[int, int]]:
    out = []
    for t in times:
        try:
            hh, mm = str(t).split(":")
            out.append((int(hh) % 24, int(mm) % 60))
        except Exception:  # noqa: BLE001
            log.warning("ignoring malformed schedule time %r", t)
    return sorted(out) or [(0, 0), (12, 0)]


def next_run_after(now: dt.datetime, times: list[tuple[int, int]]) -> dt.datetime:
    """The next wall-clock occurrence of any configured time."""
    for hh, mm in times:
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate > now:
            return candidate
    hh, mm = times[0]
    return (now + dt.timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)


class Scheduler:
    """Runs registered jobs at fixed local times each day."""

    def __init__(self, times=DEFAULT_TIMES, run_on_start: bool = False):
        self.times = _parse_times(times)
        self.run_on_start = run_on_start
        self.jobs: dict[str, Callable[[], dict]] = {}
        self.history: list[JobRun] = self._load_history()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._running: str | None = None
        self.next_run: dt.datetime | None = None

    # ------------------------------------------------------------------ jobs
    def register(self, name: str, fn: Callable[[], dict]) -> None:
        self.jobs[name] = fn

    def run_job(self, name: str) -> JobRun:
        fn = self.jobs.get(name)
        started = dt.datetime.now()
        if fn is None:
            run = JobRun(name, started.isoformat(timespec="seconds"),
                         started.isoformat(timespec="seconds"), False,
                         {"error": "no such job"})
            self._append(run)
            return run
        with self._lock:
            self._running = name
        try:
            detail = fn() or {}
            ok = True
        except Exception as exc:  # noqa: BLE001
            log.exception("scheduled job %s failed", name)
            detail, ok = {"error": str(exc)}, False
        finally:
            with self._lock:
                self._running = None
        run = JobRun(name, started.isoformat(timespec="seconds"),
                     dt.datetime.now().isoformat(timespec="seconds"), ok, detail)
        self._append(run)
        return run

    def run_all(self) -> list[JobRun]:
        return [self.run_job(name) for name in list(self.jobs)]

    # ----------------------------------------------------------------- loop
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            if self.run_on_start:
                self.run_all()
            while not self._stop.is_set():
                now = dt.datetime.now()
                self.next_run = next_run_after(now, self.times)
                wait = max((self.next_run - now).total_seconds(), 1.0)
                # Wake at least hourly so a suspended machine still catches up.
                if self._stop.wait(min(wait, 3600.0)):
                    return
                if dt.datetime.now() >= (self.next_run or now):
                    self.run_all()

        self._thread = threading.Thread(target=loop, name="gridiron-scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler started; times=%s", self.times)

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        return {
            "running": self.running,
            "times": [f"{h:02d}:{m:02d}" for h, m in self.times],
            "next_run": self.next_run.isoformat(timespec="seconds") if self.next_run else None,
            "current_job": self._running,
            "jobs": list(self.jobs),
            "history": [r.as_dict() for r in self.history[-12:]][::-1],
        }

    # --------------------------------------------------------------- history
    def _append(self, run: JobRun) -> None:
        self.history.append(run)
        self.history = self.history[-MAX_HISTORY:]
        try:
            HISTORY_PATH.write_text(json.dumps([r.as_dict() for r in self.history], indent=2))
        except Exception as exc:  # noqa: BLE001
            log.debug("could not persist scheduler history: %s", exc)

    @staticmethod
    def _load_history() -> list[JobRun]:
        if not HISTORY_PATH.exists():
            return []
        try:
            return [JobRun(**r) for r in json.loads(HISTORY_PATH.read_text())]
        except Exception:  # noqa: BLE001
            return []


def build_default_scheduler(times=DEFAULT_TIMES, n_sims: int = 5000,
                            router=None) -> Scheduler:
    """Scheduler wired to the standard refresh jobs."""
    from . import pipeline

    sched = Scheduler(times=times)
    sched.register("refresh_data", lambda: pipeline.refresh_data(force=True))

    def rebuild():
        arts = pipeline.build_all(n_sims=n_sims)
        return {"players": arts.board.height, **{k: v for k, v in arts.meta.items()
                                                 if k in ("seconds", "n_sims", "built_at")}}

    sched.register("rebuild_projections", rebuild)

    if router is not None:
        def snapshot_markets():
            markets = router.list_markets()
            n = router.poll_once(markets)
            return {"markets": len(markets), "books_snapshotted": n}

        sched.register("snapshot_markets", snapshot_markets)

    return sched
