"""macOS: drives from ``diskutil``, raw reads from ``/dev/rdiskN``.

``diskutil`` lists every whole disk with its size, name, connection and volumes. Reading goes
through the raw device (``/dev/rdiskN``: unbuffered, and much faster than ``/dev/diskN``), which
only root may open, so the app asks for administrator rights when it starts, as on Windows.
Every call to ``diskutil`` has a time limit: a failing drive can make it hang.
"""

from __future__ import annotations

import logging
import os
import plistlib
import re
import subprocess
import xml.parsers.expat
from typing import Any

from ..errors import E_OPEN_DENIED, DeviceOpenError
from .base import NO_MEDIA, NOT_ANSWERING, DeviceInfo, HealthInfo
from .posix import PosixDevice, _ioctl_int

log = logging.getLogger("lifeboat.device")

DKIOCGETBLOCKSIZE = 0x40046418          # _IOR('d', 24, uint32_t)
DKIOCGETBLOCKCOUNT = 0x40086419         # _IOR('d', 25, uint64_t)
DKIOCGETPHYSICALBLOCKSIZE = 0x4004644D  # _IOR('d', 77, uint32_t)
TIMEOUT = 20.0                          # seconds for one diskutil call

Plist = dict[str, Any]

# The drive list is refreshed every few seconds. "diskutil info" also asks each drive for its
# SMART status: not something to do to a failing drive every few seconds. So it is asked once
# per drive, and again only when that drive's entry in "diskutil list" changes.
_info_cache: dict[str, tuple[tuple, Plist]] = {}
_system_cache: set[str] | None = None


def run_plist(args: list[str], timeout: float = TIMEOUT) -> Any:
    """Run a command that prints a property list; None if it fails, hangs or prints nonsense."""
    try:
        done = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("%s did not answer: %s", " ".join(args), exc)
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    try:
        return plistlib.loads(done.stdout)
    except (plistlib.InvalidFileException, ValueError, xml.parsers.expat.ExpatError):
        log.warning("%s printed something that isn't a property list", " ".join(args))
        return None


def whole_disk(identifier: str) -> str:
    """``disk4s2`` -> ``disk4``; ``/dev/rdisk4`` -> ``disk4``."""
    match = re.search(r"(disk\d+)", identifier or "")
    return match.group(1) if match else ""


def disk_info(identifier: str) -> Plist:
    found = run_plist(["diskutil", "info", "-plist", identifier])
    return found if isinstance(found, dict) else {}


def physical_disks_of(info: Plist) -> set[str]:
    """The physical whole disks behind a volume (APFS volumes live in a container on another disk)."""
    # "diskutil info" names a store "APFSPhysicalStore", "diskutil list" names it "DeviceIdentifier".
    disks = {whole_disk(str(store.get("APFSPhysicalStore") or store.get("DeviceIdentifier") or ""))
             for store in info.get("APFSPhysicalStores") or [] if isinstance(store, dict)}
    disks.discard("")
    if not disks:
        parent = whole_disk(str(info.get("ParentWholeDisk", "")))
        if parent:
            disks.add(parent)
    return disks


def system_disks() -> set[str]:
    """Whole disks holding the running macOS (and its APFS container). They don't change."""
    global _system_cache
    if _system_cache is None:
        info = disk_info("/")
        disks = physical_disks_of(info)
        parent = whole_disk(str(info.get("ParentWholeDisk", "")))
        if parent:
            disks.add(parent)
        if not info:
            return disks  # diskutil didn't answer: ask again next time
        _system_cache = disks
    return set(_system_cache)


def _signature(entry: Plist) -> tuple:
    parts = [(p.get("DeviceIdentifier"), p.get("Size"), p.get("VolumeName"), p.get("MountPoint"))
             for p in list(entry.get("Partitions") or []) + list(entry.get("APFSVolumes") or [])
             if isinstance(p, dict)]
    return entry.get("Size"), entry.get("Content"), tuple(parts)


def cached_disk_info(entry: Plist) -> Plist:
    """``diskutil info`` for a whole disk, asked again only when its listing changed."""
    ident = str(entry.get("DeviceIdentifier", ""))
    signature = _signature(entry)
    cached = _info_cache.get(ident)
    if cached is not None and cached[0] == signature:
        return cached[1]
    info = disk_info(ident)
    if info:
        _info_cache[ident] = (signature, info)
    return info


def _volume_names(entry: Plist) -> list[str]:
    names: list[str] = []
    for part in list(entry.get("Partitions") or []) + list(entry.get("APFSVolumes") or []):
        if not isinstance(part, dict):
            continue
        name = part.get("VolumeName") or part.get("MountPoint") or ""
        if name and name not in names:
            names.append(str(name))
    if entry.get("VolumeName") and entry["VolumeName"] not in names:
        names.append(str(entry["VolumeName"]))
    return names


def is_logical(info: Plist) -> bool:
    """APFS containers are logical disks made from a partition of a physical disk: skip them.
    Attached disk images are virtual too, but they are a source like any drive."""
    return info.get("VirtualOrPhysical") == "Virtual" and info.get("BusProtocol") != "Disk Image"


def describe_disk(entry: Plist, info: Plist, system: set[str]) -> DeviceInfo | None:
    """One whole disk from ``diskutil list`` (``entry``) and ``diskutil info`` (``info``)."""
    ident = str(entry.get("DeviceIdentifier") or info.get("DeviceIdentifier") or "")
    if not ident or is_logical(info):
        return None
    size = int(info.get("TotalSize") or info.get("Size") or entry.get("Size") or 0)
    block = int(info.get("DeviceBlockSize") or 512)
    model = str(info.get("MediaName") or info.get("IORegistryEntryName") or "").strip()
    model = re.sub(r"\s+Media$", "", model) or ident
    bus = str(info.get("BusProtocol") or "")
    if bus == "PCI-Express" and info.get("SolidState"):
        bus = "NVMe"
    # Removable media (cards, discs), not "ejectable": every external USB drive is ejectable.
    removable = bool(info.get("Removable") or info.get("RemovableMedia"))
    number = int(ident[4:]) if ident[4:].isdigit() else None
    notes = []
    if not info:
        notes.append("macOS didn't say more about this drive (diskutil didn't answer in time).")
    if info.get("SMARTStatus") == "Failing":
        notes.append("The drive's own health check (SMART) says it is failing: image it first.")
    device = DeviceInfo(path=f"/dev/r{ident}", kind="disk", size=size, sector_size=block,
                        physical_sector_size=block, model=model, bus=bus, removable=removable,
                        system=ident in system, disk_number=number, volumes=_volume_names(entry), notes=notes)
    if size <= 0:
        device.status = NO_MEDIA if removable else NOT_ANSWERING
        device.notes = ["macOS detects this drive, but it doesn't report a size, so it can't be read yet. "
                        "Run a Drive check for what to try."]
    return device


def list_devices() -> list[DeviceInfo]:
    listing = run_plist(["diskutil", "list", "-plist"])
    if not isinstance(listing, dict):
        return []
    system = system_disks()
    devices = []
    for entry in listing.get("AllDisksAndPartitions") or []:
        if not isinstance(entry, dict) or not entry.get("DeviceIdentifier"):
            continue
        device = describe_disk(entry, cached_disk_info(entry), system)
        if device is not None:
            devices.append(device)
    return devices


def health_from_info(info: Plist) -> HealthInfo | None:
    status = info.get("SMARTStatus")
    if status == "Verified":
        return HealthInfo("Good", "SMART (macOS)", ["macOS reports the drive's SMART status as verified."], [])
    if status == "Failing":
        return HealthInfo("Bad", "SMART (macOS)", ["The drive predicts its own failure (SMART)."], [])
    return None  # "Not Supported": USB enclosures usually don't pass SMART through


class MacDevice(PosixDevice):
    """A raw disk on macOS (``/dev/rdiskN``). Reads must be whole blocks, which the reader ensures."""

    def _geometry(self, fd: int) -> tuple[int, int | None, int | None]:
        block = _ioctl_int(fd, DKIOCGETBLOCKSIZE, "I")
        count = _ioctl_int(fd, DKIOCGETBLOCKCOUNT, "Q")
        if not block or count is None:
            return super()._geometry(fd)  # not a disk: an image file
        physical = _ioctl_int(fd, DKIOCGETPHYSICALBLOCKSIZE, "I") or block
        return block * count, block, max(physical, block)

    def _open(self, path: str) -> int:
        try:
            return super()._open(path)
        except DeviceOpenError as exc:
            if exc.code == E_OPEN_DENIED and getattr(os, "geteuid", lambda: 1)() == 0:
                raise DeviceOpenError(
                    f"macOS didn't let Lifeboat read {path}. Allow Lifeboat in System Settings > Privacy & "
                    "Security > Full Disk Access, then open Lifeboat again.", code=E_OPEN_DENIED) from exc
            raise

    def is_present(self) -> bool:
        # A raw device can't seek to its end to report a size; ask the disk driver instead.
        if self._fd < 0 or not os.path.exists(self._path):
            return False
        count = _ioctl_int(self._fd, DKIOCGETBLOCKCOUNT, "Q")
        if count is None:
            return super().is_present()  # not a disk: an image file
        return count > 0

    def health(self) -> HealthInfo | None:
        return health_from_info(disk_info(whole_disk(self._path)))


# --- destinations --------------------------------------------------------------------------

def _mounts() -> list[tuple[str, str, str]]:
    """(device, mount point, filesystem type) of every mounted volume, from ``mount``."""
    try:
        out = subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    mounts = []
    for line in out.splitlines():
        match = re.match(r"^(.+?) on (.+) \(([^,)]+)", line)
        if match:
            mounts.append((match.group(1), match.group(2), match.group(3).strip()))
    return mounts


def mount_of(path: str) -> tuple[str, str, str] | None:
    """The mounted volume that holds ``path`` (longest matching mount point)."""
    real = os.path.realpath(path)
    best = None
    for device, mount, fstype in _mounts():
        if (real == mount or real.startswith(mount.rstrip("/") + "/")) and (best is None or len(mount) > len(best[1])):
            best = (device, mount, fstype)
    return best


NETWORK_FILESYSTEMS = {"smbfs", "afpfs", "nfs", "webdav", "ftp", "cifs"}


def disks_for_path(path: str) -> set[str]:
    """Physical disks (``/dev/diskN``) holding the volume that ``path`` is on."""
    found = mount_of(path)
    if found is None or not found[0].startswith("/dev/"):
        return set()
    info = disk_info(found[0])
    return {f"/dev/{disk}" for disk in physical_disks_of(info) or {whole_disk(found[0])} if disk}


def volume_filesystem(path: str) -> str:
    found = mount_of(path)
    return found[2] if found else ""


def is_network_path(path: str) -> bool:
    found = mount_of(path)
    if found is None:
        return False
    return found[2] in NETWORK_FILESYSTEMS or found[0].startswith("//")
