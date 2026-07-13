"""Injectable time for everything on the safety path.

The lifecycle, observe loop, permission gate, and executor never call
``time.time()``/``time.sleep()`` directly — they take a :class:`Clock`, so the
auto-abort invariants are testable deterministically with a fake.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """now()/sleep() as an injectable seam."""

    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real wall clock."""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
