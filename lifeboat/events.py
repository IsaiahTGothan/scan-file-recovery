"""Events, progress reporting, job control and user interventions.

The engine never talks to the user interface directly.  It emits ``Event``
objects through an ``EventBus``, reports ``Progress`` through a callback, and
when it needs a decision (for example "the source drive disconnected") it
asks an ``InterventionHandler``.  The desktop app, the command line tool and
the tests each plug in their own implementations.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .errors import Cancelled, describe

log = logging.getLogger("lifeboat")


class Level(enum.IntEnum):
    DEBUG = 10
    INFO = 20
    SUCCESS = 25
    WARNING = 30
    ERROR = 40
    CRITICAL = 50


@dataclass(frozen=True)
class Event:
    level: Level
    message: str
    code: str = ""
    details: str = ""
    path: str = ""
    source: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def title(self) -> str:
        return describe(self.code).title if self.code else ""

    @property
    def hint(self) -> str:
        return describe(self.code).hint if self.code else ""


Subscriber = Callable[[Event], None]


class EventBus:
    """Thread-safe publish/subscribe channel for user-visible events.

    A failing subscriber can never break the engine: exceptions raised by
    subscribers are logged and swallowed.
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()
        self.counts: dict[Level, int] = {level: 0 for level in Level}

    def subscribe(self, callback: Subscriber) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Subscriber) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def emit(self, event: Event) -> None:
        with self._lock:
            self.counts[event.level] = self.counts.get(event.level, 0) + 1
            subscribers = list(self._subscribers)
        py_level = {
            Level.DEBUG: logging.DEBUG,
            Level.INFO: logging.INFO,
            Level.SUCCESS: logging.INFO,
            Level.WARNING: logging.WARNING,
            Level.ERROR: logging.ERROR,
            Level.CRITICAL: logging.CRITICAL,
        }[event.level]
        extra = f" [{event.code}]" if event.code else ""
        where = f" ({event.path})" if event.path else ""
        log.log(py_level, "%s%s%s%s", event.message, extra, where,
                f" | {event.details}" if event.details else "")
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                log.exception("Event subscriber failed")

    # Convenience helpers -------------------------------------------------------
    def debug(self, message: str, **kw: Any) -> None:
        self.emit(Event(Level.DEBUG, message, **kw))

    def info(self, message: str, **kw: Any) -> None:
        self.emit(Event(Level.INFO, message, **kw))

    def success(self, message: str, **kw: Any) -> None:
        self.emit(Event(Level.SUCCESS, message, **kw))

    def warning(self, message: str, **kw: Any) -> None:
        self.emit(Event(Level.WARNING, message, **kw))

    def error(self, message: str, **kw: Any) -> None:
        self.emit(Event(Level.ERROR, message, **kw))

    def critical(self, message: str, **kw: Any) -> None:
        self.emit(Event(Level.CRITICAL, message, **kw))


@dataclass
class Progress:
    """Snapshot of a long-running operation, delivered a few times per second."""

    phase: str
    done: int = 0                 # units of work done (bytes unless stated)
    total: int = 0
    item: str = ""                # current file / area
    rate: float = 0.0             # bytes per second (smoothed)
    eta: float | None = None      # seconds remaining, if known
    items_done: int = 0
    items_total: int = 0
    errors: int = 0
    warnings: int = 0
    bad_bytes: int = 0
    pass_index: int = 0
    pass_count: int = 0

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return max(0.0, min(1.0, self.done / self.total))


ProgressCallback = Callable[[Progress], None]


class RateMeter:
    """Exponentially smoothed throughput and ETA."""

    def __init__(self, smoothing: float = 0.3) -> None:
        self.smoothing = smoothing
        self.rate = 0.0
        self._last_time = time.monotonic()
        self._last_done = 0

    def reset(self, done: int = 0) -> None:
        self.rate = 0.0
        self._last_time = time.monotonic()
        self._last_done = done

    def update(self, done: int) -> float:
        now = time.monotonic()
        dt = now - self._last_time
        if dt < 0.5:
            return self.rate
        instant = max(0, done - self._last_done) / dt
        if self.rate == 0.0:
            self.rate = instant
        else:
            self.rate = self.smoothing * instant + (1 - self.smoothing) * self.rate
        self._last_time = now
        self._last_done = done
        return self.rate

    def eta(self, done: int, total: int) -> float | None:
        if self.rate <= 1 or total <= done:
            return None if total > done else 0.0
        return (total - done) / self.rate


class Throttle:
    """Allow an action at most once every ``interval`` seconds."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._next = 0.0

    def ready(self, force: bool = False) -> bool:
        now = time.monotonic()
        if force or now >= self._next:
            self._next = now + self.interval
            return True
        return False


class JobControl:
    """Cancellation and pause flags shared between the UI and a worker thread."""

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._running = threading.Event()
        self._running.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._running.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def check(self) -> None:
        """Raise ``Cancelled`` if cancelled; block while paused."""
        if self._cancel.is_set():
            raise Cancelled()
        while not self._running.wait(0.2):
            pass
        if self._cancel.is_set():
            raise Cancelled()

    def sleep(self, seconds: float) -> None:
        """Sleep that wakes up early (and raises) when the job is cancelled."""
        if self._cancel.wait(seconds):
            raise Cancelled()


class Choice(enum.Enum):
    RETRY = "retry"
    SKIP = "skip"
    ABORT = "abort"


@dataclass
class Intervention:
    """A question the engine needs answered before it can continue."""

    code: str
    title: str
    message: str
    options: tuple[Choice, ...] = (Choice.RETRY, Choice.ABORT)
    # When set, the handler polls it; returning True resolves the
    # intervention with RETRY automatically (e.g. the drive came back).
    auto_retry: Callable[[], bool] | None = None
    poll_interval: float = 2.0


class InterventionHandler:
    """Default handler used by tests and the CLI.

    It waits for ``auto_retry`` to succeed for up to ``max_wait`` seconds and
    otherwise answers with the first non-retry option (SKIP or ABORT).
    """

    def __init__(self, max_wait: float = 0.0, control: JobControl | None = None) -> None:
        self.max_wait = max_wait
        self.control = control
        self.history: list[Intervention] = []

    def request(self, intervention: Intervention) -> Choice:
        self.history.append(intervention)
        deadline = time.monotonic() + self.max_wait
        while intervention.auto_retry is not None and time.monotonic() < deadline:
            if self.control is not None and self.control.cancelled:
                return Choice.ABORT
            try:
                if intervention.auto_retry():
                    return Choice.RETRY
            except Exception:
                log.exception("auto-retry probe failed")
            time.sleep(min(intervention.poll_interval, max(0.0, deadline - time.monotonic())))
        for option in intervention.options:
            if option is not Choice.RETRY:
                return option
        return Choice.ABORT
