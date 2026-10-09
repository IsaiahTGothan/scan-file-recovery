"""Run engine jobs on worker threads and relay their events to the GUI thread."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Signal

from ..errors import Cancelled
from ..events import Choice, Event, EventBus, Intervention, InterventionHandler, JobControl, Progress

log = logging.getLogger("lifeboat.ui")


class PendingDecision:
    """An intervention waiting for the user's answer."""

    def __init__(self, intervention: Intervention) -> None:
        self.intervention = intervention
        self.answer: Choice | None = None
        self._event = threading.Event()

    def answer_with(self, choice: Choice) -> None:
        self.answer = choice
        self._event.set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


class Bridge(QObject):
    """Signals are emitted from worker threads and delivered in the GUI thread."""

    event = Signal(object)                 # Event
    progress = Signal(object)              # Progress
    intervention = Signal(object)          # PendingDecision
    intervention_resolved = Signal(object)  # PendingDecision
    finished = Signal(str, object)         # job kind, result
    failed = Signal(str, str, str)         # job kind, message, traceback
    cancelled = Signal(str)


class UiInterventionHandler(InterventionHandler):
    def __init__(self, bridge: Bridge, control: JobControl) -> None:
        super().__init__()
        self.bridge = bridge
        self.control = control

    def request(self, intervention: Intervention) -> Choice:
        self.history.append(intervention)
        pending = PendingDecision(intervention)
        self.bridge.intervention.emit(pending)
        next_probe = time.monotonic() + intervention.poll_interval
        while True:
            if pending.wait(0.25):
                assert pending.answer is not None
                return pending.answer
            if self.control.cancelled:
                self.bridge.intervention_resolved.emit(pending)
                return Choice.ABORT
            if intervention.auto_retry is not None and time.monotonic() >= next_probe:
                next_probe = time.monotonic() + intervention.poll_interval
                try:
                    ok = intervention.auto_retry()
                except Exception:
                    log.exception("auto-retry probe failed")
                    ok = False
                if ok:
                    self.bridge.intervention_resolved.emit(pending)
                    return Choice.RETRY


class Job:
    """One background operation (scan, recovery, imaging, preview...)."""

    def __init__(self, kind: str, bridge: Bridge, work: Callable[[Job], Any]) -> None:
        self.kind = kind
        self.bridge = bridge
        self.work = work
        self.control = JobControl()
        self.handler = UiInterventionHandler(bridge, self.control)
        self.thread = threading.Thread(target=self._run, name=f"lifeboat-{kind}", daemon=True)
        self.started = 0.0
        self.engine: Any = None  # the engine object, for finish_early()

    def report(self, progress: Progress) -> None:
        self.bridge.progress.emit(progress)

    def start(self) -> None:
        self.started = time.monotonic()
        self.thread.start()

    def _run(self) -> None:
        try:
            result = self.work(self)
        except Cancelled:
            self.bridge.cancelled.emit(self.kind)
        except Exception as exc:
            log.exception("Job %s failed", self.kind)
            self.bridge.failed.emit(self.kind, str(exc) or exc.__class__.__name__, traceback.format_exc())
        else:
            self.bridge.finished.emit(self.kind, result)

    @property
    def running(self) -> bool:
        return self.thread.is_alive()


def connect_events(bus: EventBus, bridge: Bridge) -> None:
    def relay(event: Event) -> None:
        bridge.event.emit(event)

    bus.subscribe(relay)
