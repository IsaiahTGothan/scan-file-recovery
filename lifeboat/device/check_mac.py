"""Drive check on macOS: Disk Utility's list, plus what the I/O Registry knows beyond it.

``diskutil`` (what Disk Utility shows) only lists drives that report their size. The I/O
Registry also holds the drives macOS made contact with that never did: a disk behind a USB
bridge that answered the bridge's questions but not "how big are you?" (no media object), and
USB storage adapters with no disk behind them at all. That is the macOS side of "it shows in
Device Manager but not in Disk Management".
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from typing import Any

from .check import CheckResult, Progress, read_test
from .macos import MacDevice, describe_disk, health_from_info, run_plist, visible_disks

Node = dict[str, Any]
_CARD_WORDS = ("reader", "card", " sd", "sd/", "mmc", "xqd", "cfexpress", "compactflash")


def _children(node: Node) -> list[Node]:
    return [c for c in node.get("IORegistryEntryChildren") or [] if isinstance(c, dict)]


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in _children(node):
        yield from _walk(child)


def _has(node: Node, classes: set[str]) -> bool:
    return any(n.get("IOObjectClass") in classes for n in _walk(node))


def _first(node: Node, *keys: str) -> str:
    for item in _walk(node):
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _interconnect(node: Node) -> str:
    for item in _walk(node):
        chars = item.get("Protocol Characteristics")
        if isinstance(chars, dict) and chars.get("Physical Interconnect"):
            return str(chars["Physical Interconnect"])
    return ""


def scsi_disks_without_media(roots: Any) -> list[CheckResult]:
    """SCSI direct-access devices (USB, UAS, Thunderbolt...) with no media object: no size, no disk."""
    out: list[CheckResult] = []
    for root in roots if isinstance(roots, list) else []:
        if not isinstance(root, dict) or _has(root, {"IOMedia"}):
            continue
        name = " ".join(p for p in (_first(root, "Vendor Identification"), _first(root, "Product Identification"))
                        if p) or "Disk with no size"
        card = any(word in f" {name.lower()}" for word in _CARD_WORDS)
        out.append(CheckResult(name=name, kind="disk", connection=_interconnect(root) or "USB", size=0,
                               removable_media=card, instance_id=str(root.get("IORegistryEntryName", ""))))
    return out


def usb_storage_without_disk(roots: Any) -> list[CheckResult]:
    """USB devices with a mass-storage interface but no disk behind it (the adapter works, the drive doesn't)."""
    out: list[CheckResult] = []
    for root in roots if isinstance(roots, list) else []:
        if not isinstance(root, dict):
            continue
        storage = [n for n in _walk(root) if n.get("bInterfaceClass") == 8]
        if not storage or _has(root, {"IOSCSIPeripheralDeviceType00", "IOMedia"}):
            continue
        name = (_first(root, "USB Product Name", "kUSBProductString", "IORegistryEntryName")
                or "USB storage device")
        vendor = _first(root, "USB Vendor Name", "kUSBVendorString")
        if vendor and not name.lower().startswith(vendor.lower()):
            name = f"{vendor} {name}"
        out.append(CheckResult(name=name, kind="adapter", connection="USB",
                               instance_id=f"{root.get('idVendor', 0):04x}:{root.get('idProduct', 0):04x}"
                               if isinstance(root.get("idVendor"), int) else ""))
    return out


def check_mac(progress: Progress | None = None, in_use: Mapping[str, int] | None = None) -> list[CheckResult]:
    busy = in_use or {}
    root_user = getattr(os, "geteuid", lambda: 1)() == 0
    results: list[CheckResult] = []
    listing = run_plist(["diskutil", "list", "-plist"])
    infos: dict[str, dict] = {}
    for entry, info in visible_disks(listing, fresh=True):  # the check asks every drive afresh
        ident = str(entry["DeviceIdentifier"])
        device = describe_disk(entry, info, set())
        if device is None:
            continue
        infos[device.path] = info
        results.append(CheckResult(name=device.display_name, connection=device.bus, path=device.path,
                                   disk_number=device.disk_number, size=device.size,
                                   removable_media=device.removable, instance_id=ident))
    results.extend(scsi_disks_without_media(run_plist(["ioreg", "-a", "-r", "-c", "IOSCSIPeripheralDeviceType00"])))
    results.extend(usb_storage_without_disk(run_plist(["ioreg", "-a", "-r", "-c", "IOUSBHostDevice"])))
    total = len(results)
    for index, result in enumerate(results):
        if progress is not None:
            progress(index, total, result.name)
        if result.kind != "disk" or not result.path or result.size <= 0:
            continue
        health = health_from_info(infos.get(result.path, {}))
        if health is not None:
            result.health = health.status
        if result.path in busy:
            result.in_use, result.tested = True, False
        elif not root_user:
            result.tested = False
        else:
            read_test(MacDevice, result)
    if progress is not None:
        progress(total, total, "")
    return results
