"""Clocks: wall time (persisted, compared only on this machine) and monotonic time (silence, due times)."""
from __future__ import annotations

import time


class Clock:
    """The real clocks."""

    def wall(self) -> float:
        return time.time()

    def mono(self) -> float:
        return time.monotonic()


class FakeClock(Clock):
    """A test clock. `advance` moves both clocks; `jump_wall` moves only the wall clock (a machine sleep: on macOS
    the monotonic clock does not advance while the machine sleeps)."""

    def __init__(self, wall: float = 0.0, mono: float = 0.0):
        self._wall = float(wall)
        self._mono = float(mono)

    def wall(self) -> float:
        return self._wall

    def mono(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._wall += seconds
        self._mono += seconds

    def jump_wall(self, seconds: float) -> None:
        self._wall += seconds
