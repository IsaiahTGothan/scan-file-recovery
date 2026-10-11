"""macOS support, tested anywhere with recorded diskutil and ioreg output."""

from __future__ import annotations

import plistlib
import sys

import pytest

pytest.importorskip("fcntl")  # the POSIX device layer

from lifeboat.device import check_mac, macos  # noqa: E402
from lifeboat.device.base import NO_MEDIA, NOT_ANSWERING  # noqa: E402
from lifeboat.device.image import MemoryDevice  # noqa: E402

TB5 = 5_000_981_078_016

LISTING = {
    "AllDisksAndPartitions": [
        {"DeviceIdentifier": "disk0", "Content": "GUID_partition_scheme", "Size": 500277790720,
         "Partitions": [{"DeviceIdentifier": "disk0s1", "Content": "Apple_APFS_ISC", "Size": 524288000},
                        {"DeviceIdentifier": "disk0s2", "Content": "Apple_APFS", "Size": 494384795648}]},
        {"DeviceIdentifier": "disk3", "Content": "EF57347C-0000-11AA-AA11-00306543ECAC", "Size": 494384795648,
         "APFSPhysicalStores": [{"DeviceIdentifier": "disk0s2"}],
         "APFSVolumes": [{"DeviceIdentifier": "disk3s1", "MountPoint": "/", "VolumeName": "Macintosh HD"}]},
        {"DeviceIdentifier": "disk4", "Content": "GUID_partition_scheme", "Size": TB5,
         "Partitions": [{"DeviceIdentifier": "disk4s2", "Content": "Apple_HFS", "VolumeName": "Seagate Backup",
                         "MountPoint": "/Volumes/Seagate Backup", "Size": TB5 - 209735680}]},
        {"DeviceIdentifier": "disk5", "Content": "FDisk_partition_scheme", "Size": 67108864,
         "Partitions": [{"DeviceIdentifier": "disk5s1", "Content": "DOS_FAT_32", "Size": 67076096}]},
        {"DeviceIdentifier": "disk6", "Size": 0},
        {"DeviceIdentifier": "disk7", "Size": 0},
    ],
}
INFO = {
    "disk0": {"DeviceIdentifier": "disk0", "MediaName": "APPLE SSD AP0512Z", "BusProtocol": "Apple Fabric",
              "Internal": True, "SolidState": True, "VirtualOrPhysical": "Physical", "DeviceBlockSize": 4096,
              "TotalSize": 500277790720, "SMARTStatus": "Verified", "Removable": False, "Ejectable": False},
    "disk3": {"DeviceIdentifier": "disk3", "MediaName": "", "BusProtocol": "Apple Fabric",
              "VirtualOrPhysical": "Virtual", "TotalSize": 494384795648, "DeviceBlockSize": 4096},
    "disk4": {"DeviceIdentifier": "disk4", "MediaName": "Seagate Portable Media", "BusProtocol": "USB",
              "Internal": False, "Ejectable": True, "Removable": False, "RemovableMedia": False,
              "VirtualOrPhysical": "Physical", "DeviceBlockSize": 512, "TotalSize": TB5,
              "SMARTStatus": "Not Supported"},
    "disk5": {"DeviceIdentifier": "disk5", "MediaName": "Apple UDIF read-only Media", "BusProtocol": "Disk Image",
              "VirtualOrPhysical": "Virtual", "DeviceBlockSize": 512, "TotalSize": 67108864, "Ejectable": True},
    "disk6": {"DeviceIdentifier": "disk6", "MediaName": "SDXC Reader Media", "BusProtocol": "Secure Digital",
              "Removable": True, "RemovableMedia": True, "VirtualOrPhysical": "Physical", "TotalSize": 0},
    "disk7": {"DeviceIdentifier": "disk7", "MediaName": "WD Elements 25A3 Media", "BusProtocol": "USB",
              "Ejectable": True, "Removable": False, "VirtualOrPhysical": "Physical", "TotalSize": 0},
    "/": {"ParentWholeDisk": "disk3", "APFSPhysicalStores": [{"APFSPhysicalStore": "disk0s2"}]},
}


@pytest.fixture
def fake_diskutil(monkeypatch):
    calls = []

    def run_plist(args, timeout=macos.TIMEOUT):
        calls.append(args)
        if args[:2] == ["diskutil", "list"]:
            return LISTING
        if args[:2] == ["diskutil", "info"]:
            return INFO.get(args[-1])
        return None

    monkeypatch.setattr(macos, "run_plist", run_plist)
    monkeypatch.setattr(check_mac, "run_plist", run_plist)
    return calls


def test_drive_list_from_diskutil(fake_diskutil):
    devices = {d.path: d for d in macos.list_devices()}
    # disk3 is the APFS container made from disk0s2: a logical disk, left out.
    assert set(devices) == {"/dev/rdisk0", "/dev/rdisk4", "/dev/rdisk5", "/dev/rdisk6", "/dev/rdisk7"}
    internal, seagate, image = devices["/dev/rdisk0"], devices["/dev/rdisk4"], devices["/dev/rdisk5"]
    assert internal.system and internal.sector_size == 4096 and internal.bus == "Apple Fabric"
    assert seagate.title == "Disk 4: Seagate Portable" and seagate.size == TB5 and seagate.bus == "USB"
    assert not seagate.removable and not seagate.system and seagate.volumes == ["Seagate Backup"]
    assert image.bus == "Disk Image" and image.readable
    assert devices["/dev/rdisk6"].status == NO_MEDIA  # a card reader without a card
    assert devices["/dev/rdisk7"].status == NOT_ANSWERING  # an external drive that reports no size
    assert not devices["/dev/rdisk7"].readable


def test_system_disks_follow_the_apfs_container(fake_diskutil):
    assert macos.system_disks() == {"disk0", "disk3"}
    assert macos.physical_disks_of(LISTING["AllDisksAndPartitions"][1]) == {"disk0"}
    assert macos.whole_disk("/dev/rdisk12s3") == "disk12" and macos.whole_disk("nothing") == ""


def test_health_from_diskutil():
    assert macos.health_from_info({"SMARTStatus": "Verified"}).status == "Good"
    assert macos.health_from_info({"SMARTStatus": "Failing"}).status == "Bad"
    assert macos.health_from_info({"SMARTStatus": "Not Supported"}) is None


def test_run_plist_survives_bad_output():
    data = plistlib.dumps({"AllDisksAndPartitions": []}).decode()
    good = macos.run_plist([sys.executable, "-c", f"import sys; sys.stdout.write({data!r})"])
    assert good == {"AllDisksAndPartitions": []}
    assert macos.run_plist([sys.executable, "-c", "print('not a plist')"]) is None
    assert macos.run_plist([sys.executable, "-c", "import sys; sys.exit(3)"]) is None
    assert macos.run_plist(["/nonexistent/diskutil"]) is None
    assert macos.run_plist([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5) is None


def test_mac_device_reads_an_image_file_too(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(bytes(range(256)) * 4096)
    device = macos.MacDevice(str(image))
    try:
        assert device.size == 256 * 4096 and device.is_present()
        assert device.read_raw(512, 512) == (bytes(range(256)) * 4)[512:1024]
    finally:
        device.close()


def _node(cls, children=(), **props):
    return {"IOObjectClass": cls, "IORegistryEntryChildren": list(children), **props}


SCSI = [
    # A working drive: its media object exists (diskutil lists it).
    _node("IOSCSIPeripheralDeviceType00",
          [_node("IOBlockStorageServices", [_node("IOMedia", **{"BSD Name": "disk4"})])],
          **{"Vendor Identification": "Seagate", "Product Identification": "Portable",
             "Protocol Characteristics": {"Physical Interconnect": "USB"}}),
    # A drive behind a USB bridge that never reported its size: no media object.
    _node("IOSCSIPeripheralDeviceType00", [_node("IOBlockStorageServices", [_node("IOBlockStorageDriver")])],
          **{"Vendor Identification": "WD", "Product Identification": "Elements 25A3",
             "Protocol Characteristics": {"Physical Interconnect": "USB"}}),
    # An empty card reader.
    _node("IOSCSIPeripheralDeviceType00", [], **{"Vendor Identification": "Generic",
                                                 "Product Identification": "SD Card Reader"}),
]
USB = [
    _node("IOUSBHostDevice", [_node("IOUSBHostInterface", [_node("IOUSBMassStorageDriver", [
        _node("IOSCSIPeripheralDeviceType00")])], bInterfaceClass=8)], **{"USB Product Name": "Portable"}),
    _node("IOUSBHostDevice", [_node("IOUSBHostInterface", [_node("IOUSBMassStorageDriver")], bInterfaceClass=8)],
          **{"USB Product Name": "USB to SATA Bridge", "USB Vendor Name": "JMicron", "idVendor": 0x152D,
             "idProduct": 0x0578}),
    _node("IOUSBHostDevice", [_node("IOUSBHostInterface", bInterfaceClass=3)], **{"USB Product Name": "Keyboard"}),
]


def test_io_registry_finds_what_diskutil_cannot():
    found = check_mac.scsi_disks_without_media(SCSI)
    assert [(r.name, r.verdict) for r in found] == [("WD Elements 25A3", "not_answering"),
                                                     ("Generic SD Card Reader", "no_media")]
    assert found[0].connection == "USB"
    adapters = check_mac.usb_storage_without_disk(USB)
    assert [(a.name, a.verdict, a.instance_id) for a in adapters] == [
        ("JMicron USB to SATA Bridge", "adapter_only", "152d:0578")]
    assert check_mac.scsi_disks_without_media(None) == [] and check_mac.usb_storage_without_disk("junk") == []


def test_mac_drive_check(fake_diskutil, monkeypatch):
    def run_plist(args, timeout=macos.TIMEOUT):
        if args[0] == "ioreg":
            return SCSI if "IOSCSIPeripheralDeviceType00" in args else USB
        return {"list": LISTING}.get(args[1]) if args[1] == "list" else INFO.get(args[-1])

    monkeypatch.setattr(check_mac, "run_plist", run_plist)
    monkeypatch.setattr(check_mac, "disk_info", lambda ident: INFO.get(ident, {}))
    opened = []
    monkeypatch.setattr(check_mac, "MacDevice", lambda path: opened.append(path) or MemoryDevice(bytes(1 << 16)))
    monkeypatch.setattr(check_mac.os, "geteuid", lambda: 0, raising=False)
    results = check_mac.check_mac(in_use={"/dev/rdisk5": 67108864})
    by_name = {r.name: r for r in results}
    assert by_name["Seagate Portable"].verdict == "readable"
    assert by_name["APPLE SSD AP0512Z"].verdict == "readable" and by_name["APPLE SSD AP0512Z"].health == "Good"
    assert by_name["Apple UDIF read-only"].verdict == "in_use"  # a job is reading it: left alone
    assert by_name["SDXC Reader"].verdict == "no_media"
    assert by_name["WD Elements 25A3"].verdict == "not_answering"
    assert by_name["JMicron USB to SATA Bridge"].verdict == "adapter_only"
    assert "/dev/rdisk5" not in opened and set(opened) == {"/dev/rdisk0", "/dev/rdisk4"}

    monkeypatch.setattr(check_mac.os, "geteuid", lambda: 501, raising=False)
    results = check_mac.check_mac()
    assert {r.name: r.verdict for r in results}["Seagate Portable"] == "not_tested"


def test_mac_destinations(monkeypatch):
    mounts = [("/dev/disk3s1s1", "/", "apfs"), ("/dev/disk3s5", "/System/Volumes/Data", "apfs"),
              ("/dev/disk8s1", "/Volumes/Backup Drive", "msdos"), ("//me@nas/share", "/Volumes/share", "smbfs")]
    monkeypatch.setattr(macos, "_mounts", lambda: mounts)
    monkeypatch.setattr(macos.os.path, "realpath", lambda p: p)
    infos = {"/dev/disk3s5": {"APFSPhysicalStores": [{"APFSPhysicalStore": "disk0s2"}], "ParentWholeDisk": "disk3"},
             "/dev/disk8s1": {"ParentWholeDisk": "disk8"}}
    monkeypatch.setattr(macos, "disk_info", lambda ident: infos.get(ident, {}))
    assert macos.mount_of("/Volumes/Backup Drive/Recovered") == mounts[2]
    assert macos.mount_of("/Users/me/Desktop") == ("/dev/disk3s1s1", "/", "apfs")
    assert macos.disks_for_path("/System/Volumes/Data/Users/me") == {"/dev/disk0"}
    assert macos.disks_for_path("/Volumes/Backup Drive/x") == {"/dev/disk8"}
    assert macos.disks_for_path("/Volumes/share/x") == set()
    assert macos.volume_filesystem("/Volumes/Backup Drive/x") == "msdos"
    assert macos.is_network_path("/Volumes/share/x") and not macos.is_network_path("/Volumes/Backup Drive")


def test_mount_output_parsing(monkeypatch):
    out = ("/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
           "devfs on /dev (devfs, local, nobrowse)\n"
           "/dev/disk4s2 on /Volumes/Seagate Backup (hfs, local, nodev, nosuid, journaled, noowners)\n"
           "//me@nas._smb._tcp.local/share on /Volumes/share (smbfs, nodev, nosuid, mounted by me)\n")

    class Done:
        stdout = out

    monkeypatch.setattr(macos.subprocess, "run", lambda *a, **k: Done())
    assert macos._mounts() == [("/dev/disk3s1s1", "/", "apfs"), ("devfs", "/dev", "devfs"),
                               ("/dev/disk4s2", "/Volumes/Seagate Backup", "hfs"),
                               ("//me@nas._smb._tcp.local/share", "/Volumes/share", "smbfs")]


def test_relaunch_with_administrator_rights_on_a_mac(monkeypatch):
    from lifeboat import elevate

    ran = []

    class Done:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(elevate.sys, "platform", "darwin")
    monkeypatch.setattr(elevate.sys, "frozen", True, raising=False)
    monkeypatch.setattr(elevate.sys, "executable", "/Applications/Lifeboat.app/Contents/MacOS/Lifeboat")
    monkeypatch.setattr(elevate.subprocess, "run", lambda args, **kw: ran.append(args) or Done())
    assert elevate.can_elevate()
    assert elevate.relaunch_elevated(['--name', 'say "hi"'])
    script = ran[0][2]
    assert ran[0][:2] == ["/usr/bin/osascript", "-e"] and script.endswith("with administrator privileges")
    assert "/Applications/Lifeboat.app/Contents/MacOS/Lifeboat --name" in script
    assert '\\"hi\\"' in script and ">/dev/null 2>&1 &" in script
    assert elevate.applescript_string('a"b\\c') == '"a\\"b\\\\c"'
