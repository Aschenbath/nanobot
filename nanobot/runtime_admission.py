"""Event-loop-local task admission during an explicitly requested maintenance window.

This gate coordinates work in one runtime. It is not an installation/data lock or
proof that another process, a channel, or an external store has stopped writing.
The update owner must still stop and flush those writers before backup/install.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from contextlib import contextmanager


class RuntimeMaintenanceError(RuntimeError):
    """New work cannot enter a runtime held for maintenance."""


class RuntimeBusyError(RuntimeError):
    """Maintenance would interrupt accepted work."""


class RuntimeAdmission:
    """Share between the gateway's bus, agent, cron and local trigger intake.

    All methods run on their owning event-loop thread. Checking, counting work
    and closing admission are synchronous: no task can enter between the idle
    check and the pause. Do not call this object from an installer worker thread.
    """

    def __init__(self) -> None:
        self._paused = False
        self._active = 0
        self._open = asyncio.Event()
        self._open.set()

    @property
    def paused(self) -> bool:
        return self._paused

    def check(self) -> None:
        if self._paused:
            raise RuntimeMaintenanceError(
                "nanobot is preparing an update. Try again after maintenance finishes."
            )

    async def wait_until_open(self) -> None:
        """Pause scheduler intake without consuming or rescheduling pending work."""
        while self._paused:
            await self._open.wait()

    @contextmanager
    def activity(self) -> Generator[None, None, None]:
        """Account for accepted work, including its awaited cleanup."""
        self.check()
        self._active += 1
        try:
            yield
        finally:
            self._active -= 1

    @contextmanager
    def pause_if_idle(self, has_pending_work: Callable[[], bool]) -> Generator[None, None, None]:
        """Refuse busy runtimes without cancelling work or leaving intake closed.

        The owner supplies a synchronous view of queued/detached work which
        outlives an ``activity`` call. Hold this context through the handoff;
        releasing it after a refused/aborted update restores prior admission.
        """
        self.check()
        if self._active or has_pending_work():
            raise RuntimeBusyError(
                "nanobot has pending work. Finish active tasks and retry the update."
            )
        self._paused = True
        self._open.clear()
        try:
            yield
        finally:
            self._paused = False
            self._open.set()
