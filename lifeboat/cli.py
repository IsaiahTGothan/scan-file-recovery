"""Command line interface (``lifeboat-cli``) for scripting and batch work.

Examples::

    lifeboat-cli devices
    lifeboat-cli scan \\\\.\\PhysicalDrive2 --list
    lifeboat-cli recover \\\\.\\PhysicalDrive2 D:\\Recovered --include "*.jpg" --include "*.docx"
    lifeboat-cli image \\\\.\\PhysicalDrive2 E:\\disk2.img

Exit codes: 0 everything recovered, 1 finished with damaged/failed files,
2 failed (including "no files match"), 3 bad arguments, 130 stopped with Ctrl+C.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import signal
import sys
import time
from typing import NoReturn

from . import __version__
from .branding import APP_FULL_NAME
from .device.base import DeviceInfo
from .device.enumerate import list_devices, open_device
from .device.image import ImageDevice
from .errors import LifeboatError
from .events import Choice, Event, EventBus, Intervention, InterventionHandler, JobControl, Level, Progress
from .fs.model import F, Node
from .imaging import ImagingJob, ImagingOptions
from .logsetup import setup_logging
from .recover import RecoveryJob, RecoveryOptions, check_destination
from .rescue.reader import ReadPolicy, RescueReader
from .rescue.sectormap import SectorMap, State
from .scan import Scanner, ScanOptions
from .util import format_duration, format_rate, format_size


class Console:
    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self._last = 0.0
        self._line = False

    def progress(self, p: Progress) -> None:
        if self.quiet:
            return
        now = time.monotonic()
        if now - self._last < 0.5:
            return
        self._last = now
        pct = f"{p.fraction * 100:5.1f}% " if p.total else ""
        bits = [f"{pct}{p.phase}"]
        if p.rate:
            bits.append(format_rate(p.rate))
        if p.eta:
            bits.append(f"{format_duration(p.eta)} left")
        if p.item:
            bits.append(p.item[-60:])
        text = " | ".join(bits)
        sys.stderr.write("\r" + text[:150].ljust(150))
        sys.stderr.flush()
        self._line = True

    def event(self, event: Event) -> None:
        if event.level < Level.INFO or (self.quiet and event.level < Level.WARNING):
            return
        if self._line:
            sys.stderr.write("\n")
            self._line = False
        tag = {Level.SUCCESS: "OK", Level.WARNING: "WARNING", Level.ERROR: "ERROR",
               Level.CRITICAL: "CRITICAL"}.get(event.level, "info")
        code = f" [{event.code}]" if event.code else ""
        sys.stderr.write(f"{tag}: {event.message}{code}\n")
        sys.stderr.flush()


class CliInterventions(InterventionHandler):
    """Wait for a disconnected drive to come back (up to ``wait`` seconds); ask on a TTY otherwise."""

    def __init__(self, control: JobControl, wait: float) -> None:
        super().__init__(max_wait=wait, control=control)

    def request(self, intervention: Intervention) -> Choice:
        sys.stderr.write(f"\n*** {intervention.title}: {intervention.message}\n")
        if intervention.auto_retry is not None:
            sys.stderr.write(f"Waiting up to {format_duration(self.max_wait)} for it to come back"
                             " (Ctrl+C to stop)...\n")
            return super().request(intervention)
        if sys.stdin.isatty():
            options = "/".join(o.value for o in intervention.options)
            answer = input(f"Choose {options}: ").strip().lower()
            for option in intervention.options:
                if option.value.startswith(answer[:1] or "x"):
                    return option
        return Choice.ABORT


def _open(source: str, sector_size: int | None) -> tuple[DeviceInfo, RescueReader, EventBus]:
    device = (ImageDevice(source, sector_size or 512, _unreadable_from_map(source))
              if os.path.isfile(source) else open_device(source))
    info = device.info
    bus = EventBus()
    reader = RescueReader(device, ReadPolicy(), events=bus)
    return info, reader, bus


def _unreadable_from_map(path: str) -> list[tuple[int, int]]:
    map_path = path + ".map"
    if not os.path.exists(map_path):
        return []
    size = os.path.getsize(path)
    sm = SectorMap.load(map_path, size - size % 512)
    return sm.ranges([State.BAD, State.FAILED, State.SKIPPED, State.UNTRIED])


def _install_ctrl_c(control: JobControl) -> None:
    def handler(_signum: int, _frame: object) -> None:
        sys.stderr.write("\nStopping (Ctrl+C)... finishing the current step.\n")
        control.cancel()

    signal.signal(signal.SIGINT, handler)


def cmd_devices(args: argparse.Namespace) -> int:
    devices = list_devices()
    if args.json:
        print(json.dumps([d.__dict__ | {"identity": d.identity} for d in devices], indent=1, default=str))
        return 0
    if not devices:
        print("No drives found (on Windows, run as administrator).")
    for d in devices:
        flags = " [system]" if d.system else ""
        vols = f"  {', '.join(d.volumes)}" if d.volumes else ""
        print(f"{d.path:<22} {d.capacity_text:>9}  {d.bus:<8} {d.display_name}{flags}{vols}")
    return 0


def _scan(args: argparse.Namespace, control: JobControl, console: Console):
    info, reader, bus = _open(args.source, args.sector_size)
    bus.subscribe(console.event)
    reader.control = control
    options = ScanOptions(carve=not getattr(args, "no_carve", False))
    scanner = Scanner(reader, info, bus, control, console.progress, CliInterventions(control, args.wait), options)
    result = scanner.deep_scan() if args.deep else scanner.quick_scan()
    if console._line:
        sys.stderr.write("\n")
    return info, reader, bus, result


def _match(node: Node, args: argparse.Namespace) -> bool:
    if node.flags & (F.SYSTEM | F.STREAM):
        return False
    if args.deleted_only and not node.flags & (F.DELETED | F.CARVED):
        return False
    if args.existing_only and node.flags & (F.DELETED | F.CARVED):
        return False
    if args.include and not any(fnmatch.fnmatch(node.name.lower(), p.lower()) for p in args.include):
        return False
    if args.exclude and any(fnmatch.fnmatch(node.name.lower(), p.lower()) for p in args.exclude):
        return False
    if not args.path:
        return True
    full = node.path().lower()
    inside = "/".join(node.path_parts()[1:]).lower()  # path within its volume
    for prefix in args.path:
        want = prefix.replace("\\", "/").strip("/").lower()
        if full.startswith(want) or inside.startswith(want):
            return True
    return False


def cmd_scan(args: argparse.Namespace) -> int:
    control = JobControl()
    _install_ctrl_c(control)
    console = Console(args.quiet)
    _info, _reader, _bus, result = _scan(args, control, console)
    files = [n for n in result.root.walk() if n.children is None and not n.flags & (F.SYSTEM | F.STREAM)]
    if args.json:
        rows = [{"path": n.path(), "size": n.size, "deleted": bool(n.flags & F.DELETED),
                 "carved": bool(n.flags & F.CARVED), "mtime": n.mtime} for n in files]
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=1)
    if args.list:
        for n in files:
            mark = "D" if n.flags & F.DELETED else " "
            print(f"{mark} {n.size:>14,}  {n.path()}")
    for vr in result.volumes:
        state = vr.volume.describe() if vr.volume else (vr.error or "not readable")
        print(f"{vr.title}: {state}")
    total, deleted, size = result.counts()
    print(f"{total:,} files ({deleted:,} deleted), {format_size(size)}")
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    control = JobControl()
    _install_ctrl_c(control)
    console = Console(args.quiet)
    info, reader, bus, result = _scan(args, control, console)
    files = [n for n in result.root.walk() if n.children is None and _match(n, args)]
    if not files:
        print("ERROR: no files match.", file=sys.stderr)
        return 2
    size = sum(n.size for n in files)
    report = check_destination(args.destination, info, size, max(n.size for n in files),
                               sum(1 for n in files if n.size > (4 << 30) - 1), allow_low_space=args.allow_low_space)
    for issue in report.issues:
        print(f"{'ERROR' if issue.blocking else 'WARNING'}: {issue.message} [{issue.code}]", file=sys.stderr)
    if not report.ok:
        return 2
    print(f"Recovering {len(files):,} files ({format_size(size)}) to {args.destination}", file=sys.stderr)
    options = RecoveryOptions(destination=args.destination, job_folder=not args.no_job_folder and not args.resume,
                              verify=not args.no_verify, thoroughness=args.thoroughness,
                              mark_damaged=args.mark_damaged, resume=args.resume)
    job = RecoveryJob(files, reader, info, options, bus, control, console.progress,
                      CliInterventions(control, args.wait))
    summary = job.run()
    if console._line:
        sys.stderr.write("\n")
    print(f"{summary.headline()} in {format_duration(summary.seconds)}")
    print(f"Saved in: {summary.job_dir}")
    if summary.report_html:
        print(f"Report:   {summary.report_html}")
    if summary.cancelled:
        return 130
    return {"success": 0, "warning": 1, "failed": 2}[summary.outcome]


def cmd_image(args: argparse.Namespace) -> int:
    control = JobControl()
    _install_ctrl_c(control)
    console = Console(args.quiet)
    info, reader, bus = _open(args.source, args.sector_size)
    bus.subscribe(console.event)
    reader.control = control
    options = ImagingOptions(args.output, thoroughness=args.thoroughness, same_drive=args.same_drive)
    job = ImagingJob(reader, info, options, bus, control, console.progress, CliInterventions(control, args.wait))
    summary = job.run()
    if console._line:
        sys.stderr.write("\n")
    print(f"{summary.percent:.3f}% rescued ({format_size(summary.good)} of {format_size(summary.size)}), "
          f"{format_size(summary.bad)} unreadable, in {format_duration(summary.seconds)}")
    print(f"Image: {summary.output}\nMap:   {summary.mapfile}")
    if summary.error:
        return 2
    if summary.cancelled:
        return 130
    return 0 if summary.outcome == "success" else 1


class _Parser(argparse.ArgumentParser):
    """Exit with 3 on bad arguments so scripts can tell them from a failed recovery (2)."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(3, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="lifeboat-cli", description=f"{APP_FULL_NAME} {__version__}")
    parser.add_argument("--version", action="version", version=f"{APP_FULL_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("devices", help="list drives")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_devices)

    def source_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("source", help=r"drive (\\.\PhysicalDrive2, /dev/sdb) or image file")
        sp.add_argument("--sector-size", type=int, choices=[512, 1024, 2048, 4096], help="image sector size")
        sp.add_argument("--wait", type=float, default=1800, help="seconds to wait for a disconnected drive")
        sp.add_argument("-q", "--quiet", action="store_true")

    p = sub.add_parser("scan", help="scan a drive or image and list what is recoverable")
    source_args(p)
    p.add_argument("--deep", action="store_true", help="also search the whole drive")
    p.add_argument("--no-carve", action="store_true", help="deep scan without file signatures")
    p.add_argument("--list", action="store_true", help="print every file")
    p.add_argument("--json", metavar="FILE", help="write the file list as JSON")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("recover", help="scan and recover files to a destination folder")
    source_args(p)
    p.add_argument("destination")
    p.add_argument("--deep", action="store_true")
    p.add_argument("--include", action="append", help="file name pattern, e.g. *.jpg (repeatable)")
    p.add_argument("--exclude", action="append", help="file name pattern to skip (repeatable)")
    p.add_argument("--path", action="append", help="only files under this path as shown by 'scan --list'")
    p.add_argument("--deleted-only", action="store_true")
    p.add_argument("--existing-only", action="store_true")
    p.add_argument("--thoroughness", choices=["quick", "standard", "maximum"], default="standard")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--no-job-folder", action="store_true", help="write straight into the destination")
    p.add_argument("--resume", action="store_true", help="continue a stopped recovery in DESTINATION")
    p.add_argument("--mark-damaged", action="store_true")
    p.add_argument("--allow-low-space", action="store_true")
    p.add_argument("--no-carve", action="store_true")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("image", help="copy a drive sector by sector to an image file (resumable)")
    source_args(p)
    p.add_argument("output")
    p.add_argument("--thoroughness", choices=["quick", "standard", "maximum"], default="standard")
    p.add_argument("--same-drive", action="store_true",
                   help="continue an existing image although its map names another drive (same drive, other adapter)")
    p.set_defaults(func=cmd_image)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging()
    try:
        return int(args.func(args))
    except LifeboatError as exc:
        print(f"ERROR: {exc.message} [{exc.code}]", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
