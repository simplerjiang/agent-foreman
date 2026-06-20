"""Process / idle watcher — liveness + CPU activity, feeding ``last_progress_at`` (DESIGN §4.1/§4.3).

The third cheap observation source (alongside the git watcher and Claude hooks). Each tick it asks
two deterministic questions about an agent's child process: **is it still alive** (PID present, not a
zombie / exited) and **did it burn any CPU since last tick** (a sign it's actually working, not hung).
Fresh CPU activity counts as progress, so it calls ``tracker.touch(key)`` — the same signal a git
diff or a stdout line raises — letting the Supervisor watchdog (T2.6) tell "still working" from
"alive but stalled" without spending a token.

The testable core is ``poll()`` — one deterministic comparison of the current sample to the last
seen one; ``watch()`` is just a loop calling it on an interval. ``psutil`` is reached only through an
injectable ``sampler`` seam (same discipline as GitWatcher's ``runner``), so tests need no real
process. The first poll of a key establishes a baseline and never reports activity (we only report
*change* in cumulative CPU time).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

# DESIGN §4.1: the cheap deterministic poll runs every 10–30s. Process liveness/CPU changes slowly,
# so the slower end of that range is fine; callers may override per agent type (Codex tighter, §4.1).
DEFAULT_INTERVAL_S = 15.0

# A tick is "active" if cumulative CPU time grew by more than this many seconds. A tiny epsilon
# absorbs float noise yet still catches a process that did any real work between polls.
DEFAULT_ACTIVE_THRESHOLD_S = 0.01


@dataclass
class ProcSample:
    """A single observation of a process at one instant."""

    alive: bool          # PID present and not a zombie / exited
    cpu_seconds: float    # cumulative user+system CPU time (monotonic while the process lives)


@dataclass
class ProcStatus:
    """The poll verdict the Supervisor reads. ``alive`` is None when this tick couldn't be sampled."""

    alive: bool | None
    active: bool          # CPU advanced since last tick (always False on the baseline tick)


Sampler = Callable[[int], ProcSample | None]


def _default_sampler(pid: int) -> ProcSample | None:
    """Sample a process via psutil. Returns None when it can't be read this tick (transient).

    NoSuchProcess → the process is gone (``alive=False``). AccessDenied / other psutil hiccups →
    None, meaning "don't know this tick" so a momentary read failure isn't mistaken for death.
    """
    import psutil  # local import: psutil is a client-only dep, keep module import cheap/optional

    try:
        p = psutil.Process(pid)
        with p.oneshot():
            if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
                return ProcSample(alive=False, cpu_seconds=0.0)
            t = p.cpu_times()
            return ProcSample(alive=True, cpu_seconds=float(t.user + t.system))
    except psutil.NoSuchProcess:
        return ProcSample(alive=False, cpu_seconds=0.0)
    except (psutil.AccessDenied, psutil.Error):
        return None


class ProcessWatcher:
    def __init__(
        self,
        tracker=None,
        *,
        sampler: Sampler = _default_sampler,
        active_threshold_s: float = DEFAULT_ACTIVE_THRESHOLD_S,
    ) -> None:
        self.tracker = tracker                 # optional ProgressTracker: CPU activity → touch(key)
        self._sampler = sampler
        self._threshold = active_threshold_s
        # per key: (pid, last live sample). The pid is kept so a CPU delta is only computed within
        # the *same* process — a restart (or OS PID reuse) under the same key re-baselines instead
        # of subtracting two unrelated processes' CPU times (which could fake a progress touch).
        self._seen: dict[str, tuple[int, ProcSample]] = {}

    def poll(self, key: str, pid: int) -> ProcStatus:
        """Sample ``pid`` once; touch the tracker iff it burned CPU since the previous live tick.

        The first poll of a ``key`` records a baseline (``active=False``). A dead/gone process drops
        its baseline so a later restart re-baselines cleanly rather than comparing across PIDs; a
        change of ``pid`` under the same ``key`` likewise re-baselines.
        """
        sample = self._sampler(pid)
        if sample is None:  # couldn't read this tick — report unknown, leave baseline untouched
            return ProcStatus(alive=None, active=False)

        if not sample.alive:
            self._seen.pop(key, None)
            return ProcStatus(alive=False, active=False)

        prev = self._seen.get(key)
        self._seen[key] = (pid, sample)
        active = (
            prev is not None
            and prev[0] == pid  # only compare CPU within the same process
            and (sample.cpu_seconds - prev[1].cpu_seconds) > self._threshold
        )
        if active and self.tracker is not None:
            self.tracker.touch(key)  # CPU progress counts the same as a git diff / stdout line (§4.1)
        return ProcStatus(alive=True, active=active)

    async def watch(
        self,
        key: str,
        pid: int,
        *,
        interval: float = DEFAULT_INTERVAL_S,
    ) -> None:
        """Poll ``pid`` forever on ``interval`` seconds (cancel the task to stop)."""
        while True:
            self.poll(key, pid)
            await asyncio.sleep(interval)

    def drop(self, key: str) -> None:
        """Forget an agent's baseline (e.g. once it has fully stopped). No-op if unknown."""
        self._seen.pop(key, None)
