"""What Device Manager sees: storage devices from Windows Plug and Play (SetupAPI).

Device Manager lists hardware Windows has made contact with, Disk Management
only disks that answer. This module reads the Device Manager side: every disk
drive, every USB storage adapter (and whether a disk sits behind it), and
storage-related devices with an error code, plus the disk number of each disk
that Windows has a device for.
"""

from __future__ import annotations

import ctypes
import struct
import sys
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field

if sys.platform != "win32":  # pragma: no cover - imported only on Windows
    raise ImportError("lifeboat.device.pnp_windows is only available on Windows")

from .windows import IOCTL_STORAGE_GET_DEVICE_NUMBER, CloseHandle, DeviceOpenError, _ioctl, _open

_setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
_cfgmgr = ctypes.WinDLL("cfgmgr32", use_last_error=True)

DWORD = wintypes.DWORD
ULONG = ctypes.c_ulong
HDEVINFO = ctypes.c_void_p
INVALID_HANDLE = ctypes.c_void_p(-1).value

DIGCF_PRESENT = 0x2
DIGCF_ALLCLASSES = 0x4
DIGCF_DEVICEINTERFACE = 0x10
SPDRP_DEVICEDESC = 0x00
SPDRP_SERVICE = 0x04
SPDRP_CLASS = 0x07
SPDRP_FRIENDLYNAME = 0x0C
SPDRP_ENUMERATOR_NAME = 0x16
DN_HAS_PROBLEM = 0x00000400
CR_SUCCESS = 0

GUID_DEVINTERFACE_DISK = "{53f56307-b6bf-11d0-94f2-00a0c91efb8b}"
STORAGE_SERVICES = {"usbstor", "uaspstor"}          # USB mass storage, USB Attached SCSI
WATCHED_CLASSES = {"diskdrive", "usb", "usbdevice", "scsiadapter", "hdc"}


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> GUID:
        guid = cls()
        ctypes.memmove(ctypes.byref(guid), uuid.UUID(text).bytes_le, 16)
        return guid


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("ClassGuid", GUID), ("DevInst", DWORD), ("Reserved", ctypes.c_size_t)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("InterfaceClassGuid", GUID), ("Flags", DWORD), ("Reserved", ctypes.c_size_t)]


def _fn(dll: ctypes.WinDLL, name: str, argtypes: list[object], restype: object) -> object:
    func = getattr(dll, name)
    func.argtypes = argtypes
    func.restype = restype
    return func


_P_DEVINFO = ctypes.POINTER(SP_DEVINFO_DATA)
SetupDiGetClassDevsW = _fn(_setupapi, "SetupDiGetClassDevsW",
                           [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, DWORD], HDEVINFO)
SetupDiEnumDeviceInfo = _fn(_setupapi, "SetupDiEnumDeviceInfo", [HDEVINFO, DWORD, _P_DEVINFO], wintypes.BOOL)
SetupDiGetDeviceRegistryPropertyW = _fn(
    _setupapi, "SetupDiGetDeviceRegistryPropertyW",
    [HDEVINFO, _P_DEVINFO, DWORD, ctypes.POINTER(DWORD), ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD)],
    wintypes.BOOL)
SetupDiGetDeviceInstanceIdW = _fn(_setupapi, "SetupDiGetDeviceInstanceIdW",
                                  [HDEVINFO, _P_DEVINFO, wintypes.LPWSTR, DWORD, ctypes.POINTER(DWORD)],
                                  wintypes.BOOL)
SetupDiEnumDeviceInterfaces = _fn(
    _setupapi, "SetupDiEnumDeviceInterfaces",
    [HDEVINFO, _P_DEVINFO, ctypes.POINTER(GUID), DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)], wintypes.BOOL)
SetupDiGetDeviceInterfaceDetailW = _fn(
    _setupapi, "SetupDiGetDeviceInterfaceDetailW",
    [HDEVINFO, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD),
     _P_DEVINFO], wintypes.BOOL)
SetupDiDestroyDeviceInfoList = _fn(_setupapi, "SetupDiDestroyDeviceInfoList", [HDEVINFO], wintypes.BOOL)
CM_Get_DevNode_Status = _fn(_cfgmgr, "CM_Get_DevNode_Status",
                            [ctypes.POINTER(ULONG), ctypes.POINTER(ULONG), DWORD, ULONG], ULONG)
CM_Get_Child = _fn(_cfgmgr, "CM_Get_Child", [ctypes.POINTER(DWORD), DWORD, ULONG], ULONG)
CM_Get_Sibling = _fn(_cfgmgr, "CM_Get_Sibling", [ctypes.POINTER(DWORD), DWORD, ULONG], ULONG)
CM_Get_Device_IDW = _fn(_cfgmgr, "CM_Get_Device_IDW", [DWORD, wintypes.LPWSTR, ULONG, ULONG], ULONG)


@dataclass
class PnpDevice:
    instance_id: str
    name: str
    device_class: str               # "DiskDrive", "USB", ...
    service: str                    # "disk", "USBSTOR", "UASPStor", ...
    enumerator: str                 # "USBSTOR", "SCSI", "USB", "NVME", ...
    problem: int = 0                # Device Manager error code, 0 when fine
    disk_number: int | None = None  # PhysicalDrive number, when Windows created a disk for it
    children: list[str] = field(default_factory=list)

    @property
    def is_disk(self) -> bool:
        return self.device_class.lower() == "diskdrive"

    @property
    def is_usb_storage(self) -> bool:
        return self.service.lower() in STORAGE_SERVICES


def _new_devinfo() -> SP_DEVINFO_DATA:
    data = SP_DEVINFO_DATA()
    data.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
    return data


def _property(hdev: int, data: SP_DEVINFO_DATA, prop: int) -> str:
    required = DWORD(0)
    buf = ctypes.create_string_buffer(1024)
    ok = SetupDiGetDeviceRegistryPropertyW(hdev, ctypes.byref(data), prop, None, buf, len(buf),
                                           ctypes.byref(required))
    if not ok and required.value > len(buf):
        buf = ctypes.create_string_buffer(required.value)
        ok = SetupDiGetDeviceRegistryPropertyW(hdev, ctypes.byref(data), prop, None, buf, len(buf),
                                               ctypes.byref(required))
    if not ok:
        return ""
    # REG_SZ or REG_MULTI_SZ: the first string is the one Device Manager shows.
    return buf.raw[:required.value].decode("utf-16-le", "replace").split("\x00", 1)[0].strip()


def _instance_id(hdev: int, data: SP_DEVINFO_DATA) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    if SetupDiGetDeviceInstanceIdW(hdev, ctypes.byref(data), buf, len(buf), None):
        return buf.value.upper()
    return ""


def _problem(devinst: int) -> int:
    status, problem = ULONG(0), ULONG(0)
    if CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), devinst, 0) != CR_SUCCESS:
        return 0
    return int(problem.value) if status.value & DN_HAS_PROBLEM else 0


def _children(devinst: int) -> list[str]:
    ids: list[str] = []
    child = DWORD(0)
    if CM_Get_Child(ctypes.byref(child), devinst, 0) != CR_SUCCESS:
        return ids
    for _ in range(64):
        buf = ctypes.create_unicode_buffer(1024)
        if CM_Get_Device_IDW(child.value, buf, len(buf), 0) == CR_SUCCESS:
            ids.append(buf.value.upper())
        sibling = DWORD(0)
        if CM_Get_Sibling(ctypes.byref(sibling), child.value, 0) != CR_SUCCESS:
            break
        child = sibling
    return ids


def _device_number(path: str) -> int | None:
    try:
        handle = _open(path, 0)
    except DeviceOpenError:
        return None
    try:
        blob = _ioctl(handle, IOCTL_STORAGE_GET_DEVICE_NUMBER, None, 12)
    finally:
        CloseHandle(handle)
    if not blob or len(blob) < 8:
        return None
    _device_type, number = struct.unpack_from("<II", blob, 0)
    return int(number)


def disk_numbers() -> dict[str, int]:
    """Device instance ID -> PhysicalDrive number, for every disk Windows created a disk device for."""
    guid = GUID.parse(GUID_DEVINTERFACE_DISK)
    hdev = SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not hdev or hdev == INVALID_HANDLE:
        return {}
    numbers: dict[str, int] = {}
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    try:
        for index in range(256):
            iface = SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), index, ctypes.byref(iface)):
                break
            required = DWORD(0)
            SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(iface), None, 0, ctypes.byref(required), None)
            if required.value < 6:
                continue
            detail = ctypes.create_string_buffer(required.value + 2)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize: the size of the fixed part, padded.
            struct.pack_into("<I", detail, 0, 8 if pointer_size == 8 else 6)
            data = _new_devinfo()
            if not SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(iface), detail, required.value, None,
                                                    ctypes.byref(data)):
                continue
            path = ctypes.wstring_at(ctypes.addressof(detail) + 4)
            number = _device_number(path)
            if number is not None:
                numbers[_instance_id(hdev, data)] = number
    finally:
        SetupDiDestroyDeviceInfoList(hdev)
    return numbers


def storage_devices() -> list[PnpDevice]:
    """Disk drives, USB storage adapters and storage-related devices with an error, as Device Manager sees them."""
    numbers = disk_numbers()
    hdev = SetupDiGetClassDevsW(None, None, None, DIGCF_ALLCLASSES | DIGCF_PRESENT)
    if not hdev or hdev == INVALID_HANDLE:
        raise OSError(ctypes.get_last_error(), "Windows did not return its device list")
    devices: list[PnpDevice] = []
    try:
        for index in range(100_000):
            data = _new_devinfo()
            if not SetupDiEnumDeviceInfo(hdev, index, ctypes.byref(data)):
                break
            device_class = _property(hdev, data, SPDRP_CLASS)
            service = _property(hdev, data, SPDRP_SERVICE)
            problem = _problem(data.DevInst)
            watched = device_class.lower() == "diskdrive" or service.lower() in STORAGE_SERVICES
            if not watched and not (problem and device_class.lower() in WATCHED_CLASSES):
                continue
            instance = _instance_id(hdev, data)
            name = (_property(hdev, data, SPDRP_FRIENDLYNAME) or _property(hdev, data, SPDRP_DEVICEDESC)
                    or instance)
            devices.append(PnpDevice(
                instance_id=instance, name=name, device_class=device_class, service=service,
                enumerator=_property(hdev, data, SPDRP_ENUMERATOR_NAME), problem=problem,
                disk_number=numbers.get(instance),
                children=_children(data.DevInst) if service.lower() in STORAGE_SERVICES else []))
    finally:
        SetupDiDestroyDeviceInfoList(hdev)
    return devices
