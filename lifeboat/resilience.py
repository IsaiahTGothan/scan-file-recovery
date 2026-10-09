"""Keep long jobs alive across disconnects and hangs of the source drive."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

from .errors import E_SOURCE_GONE, E_SOURCE_HUNG, Cancelled, DeviceGoneError, DeviceHungError
from .events import Choice, EventBus, Intervention, InterventionHandler, JobControl
from .rescue.reader import RescueReader

log = logging.getLogger("lifeboat")
T = TypeVar("T")


def try_reattach(reader: RescueReader) -> bool:
    """True when the source answers again (reopening it if needed)."""
    device = reader.device
    original_size = device.size
    try:
        if device.is_present():
            return True
        if device.reopen() and device.is_present():
            if device.size != original_size:
                log.warning("Reconnected device has a different size; refusing to continue")
                return False
            return True
    except Exception:
        log.exception("Reattach attempt failed")
    return False


def run_with_device_retry(
    action: Callable[[], T],
    reader: RescueReader,
    handler: InterventionHandler,
    events: EventBus,
    control: JobControl | None,
    doing: str,
) -> T:
    """Run ``action``; when the source disconnects or hangs, ask what to do.

    The source is re-probed automatically every couple of seconds while the
    question is open, so plugging the drive back in resumes on its own.
    Because the sector map remembers what was already read, repeating the
    interrupted step is cheap.
    """
    while True:
        if control is not None:
            control.check()
        try:
            return action()
        except DeviceGoneError as exc:
            events.critical(
                f"The source drive disconnected while {doing}. Reconnect it to continue.",
                code=E_SOURCE_GONE, details=str(exc),
            )
            choice = handler.request(Intervention(
                code=E_SOURCE_GONE,
                title="Source drive disconnected",
                message=(f"The drive stopped answering while {doing}. Reconnect it (try another "
                         "USB port or cable). Lifeboat resumes automatically when it is back."),
                options=(Choice.RETRY, Choice.ABORT),
                auto_retry=lambda: try_reattach(reader),
            ))
            if choice is not Choice.RETRY or not try_reattach(reader):
                raise Cancelled("Stopped because the source drive disconnected.") from exc
            events.success("The source drive is back. Continuing where Lifeboat stopped.")
        except DeviceHungError as exc:
            events.critical(
                f"The source drive stopped responding while {doing}.",
                code=E_SOURCE_HUNG, details=str(exc),
            )
            choice = handler.request(Intervention(
                code=E_SOURCE_HUNG,
                title="Source drive not responding",
                message=("The drive is not answering. Press Retry to keep trying, or unplug it, "
                         "wait 10 seconds and reconnect it - Lifeboat continues automatically."),
                options=(Choice.RETRY, Choice.ABORT),
                auto_retry=lambda: try_reattach(reader) and _answers(reader),
            ))
            if choice is not Choice.RETRY:
                raise Cancelled("Stopped because the source drive stopped responding.") from exc
            events.info("Retrying the source drive.")


def _answers(reader: RescueReader) -> bool:
    try:
        reader.device.read_raw(0, reader.device.sector_size, timeout=5.0)
        return True
    except Exception:  # noqa: BLE001
        return False
