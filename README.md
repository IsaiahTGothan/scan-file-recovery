<p align="center">
  <img src="lifeboat/assets/lifeboat.png" width="96" alt="Lifeboat icon">
</p>

<h1 align="center">Lifeboat Data Recovery</h1>
<p align="center"><b>Get your files off a failing drive.</b><br>
Read-only, bad-sector aware, and it tells you about every problem.<br>
by Zays</p>

---

Lifeboat reads drives that Windows can't open anymore, shows you every file it can find (including deleted ones), and copies the files you pick to a **different, healthy drive**. It was built for the situations where other tools crawl or give up:

* **Built for dying drives.** Healthy data comes off first in one sweep. Damaged areas are skipped, and Lifeboat only goes back for them at the end, sector by sector. Every area's state is remembered, so a bad sector is never hammered over and over. That retry storm is what makes some tools take forever and finish off weak drives.
* **Disk-order copying.** Files are copied in the order they sit on the disk, so a hard drive reads mostly front to back instead of seeking back and forth.
* **Independent of Windows Explorer.** Lifeboat reads the raw disk itself, so it doesn't care about file permissions, paths over 260 characters, names Windows can't handle (`CON`, `aux.txt`, `what?.txt`, trailing dots), or a drive Windows says needs formatting.
* **Nothing fails silently.** Every file ends up **Recovered**, **Damaged** (with the exact unreadable byte ranges), **Failed** (with the reason), or **Not processed**. Every copy is read back from the destination and checked with SHA-256. Problems show up as pop-ups, as a red banner when Lifeboat needs you, in the Problems tab, as a Windows notification and a sound, and in an HTML + CSV report saved next to your files.
* **Survives disconnects.** If the failing drive drops off USB, or the destination fills up or disappears, Lifeboat pauses and tells you. When the drive comes back it continues on its own. Stopped recoveries and disk images can be resumed later.

## Contents

- [Download and install](#download-and-install)
- [Recovering files: the 4 steps](#recovering-files-the-4-steps)
- [When the drive is really failing](#when-the-drive-is-really-failing)
- [What the results mean](#what-the-results-mean)
- [Notifications and error codes](#notifications-and-error-codes)
- [What Lifeboat can read](#what-lifeboat-can-read)
- [Command line](#command-line)
- [Adding the owner logo](#adding-the-owner-logo)
- [Building from source](#building-from-source)
- [How it works](#how-it-works)

## Download and install

Every push to this repository builds and tests Lifeboat on Windows. To get the installer:

1. Open the repository's **Actions** tab, then the latest successful **Build and test** run.
2. Under **Artifacts**, download **Lifeboat-Windows**. It contains:
   * `Lifeboat-Setup-1.0.0.exe`: the installer (Start menu entry, optional desktop icon, uninstaller).
   * `Lifeboat-1.0.0-portable.zip`: the same app with no installation. Unzip it and run `Lifeboat.exe`.

Pushing a version tag (for example `v1.0.0`) also publishes both files as a GitHub Release.

Lifeboat asks for **administrator rights** when it starts (the UAC prompt). Windows only allows direct disk access to administrators. Because the app isn't code-signed yet, SmartScreen may say "Windows protected your PC". Click **More info → Run anyway**.

Requirements: Windows 10 or 11, 64-bit.

## Recovering files: the 4 steps

1. **Pick the drive.** Connect the failing drive and select it under **Sources**. The overview shows its model, capacity, sector size, connection and, when Windows can read it, its SMART health.
2. **Scan it.** Click **Scan**. Lifeboat reads the partition table and the file tables (NTFS MFT, FAT, exFAT) and rebuilds the folders, including deleted files and folders. If partitions are missing or the drive was formatted, use **Deep scan**: it reads the whole drive to find lost partitions and to recognise files by their content.
3. **Tick your files.** Browse folders, search (`*.jpg`, `invoice`), filter by type (photos, videos, documents...) or by existing/deleted, and press **Preview** to look inside a file first. The **Chance** column estimates how recoverable each file is.
4. **Recover.** Click **Recover N files**, choose a folder on a different drive, and pick how hard to try:
   * **Quick**: copy what reads easily and never retry.
   * **Standard** (recommended): then go back for the skipped and damaged areas, sector by sector.
   * **Maximum**: Standard, plus three more tries on every bad sector.

   Before anything is written, Lifeboat checks the destination. It refuses a folder on the drive being recovered, warns when space is short, and warns about FAT32's 4 GB file limit. Each recovery gets its own dated folder containing your files, `Lifeboat Report.html` and `Lifeboat Report.csv`.

You can **Pause**, **Stop** (everything recovered so far is kept) or **Finish now** (skip the remaining retry passes) at any time. To continue a stopped recovery, start a recovery into the same folder and tick **Resume**. Files that were already verified are skipped.

## When the drive is really failing

* **If Windows offers to format the drive or "scan and fix" it, click Cancel.** Both write to the drive and can destroy what's left.
* **Clicking, beeping, very slow, or keeps disconnecting? Make a disk image first.** **Create image** copies the whole drive to a file on a healthy drive (it needs as much free space as the failing drive's size). The healthy areas are copied first. The progress map (`.map`, compatible with GNU ddrescue) means you can stop and resume. Then open the image with **Open image** and recover from it without touching the failing drive again. Files recovered from the image are still marked Damaged wherever the drive couldn't be read.
* **Keep File Explorer, antivirus scans and backup software away from the failing drive** while Lifeboat works. They compete for the same weak heads.
* **USB adapters matter.** If a drive keeps dropping off or shows the wrong size, try another cable, port or adapter. Lifeboat detects when an enclosure used 4 KB sectors and the dock shows 512-byte sectors (or the reverse) and adjusts automatically.
* **A drive that doesn't spin up or isn't detected at all** has a hardware fault (PCB, heads, motor). No software can read it; it needs a clean-room lab.
* **BitLocker drives:** unlock the drive in Windows (password or recovery key), then select its **drive letter** under *Volumes*. Lifeboat then reads the decrypted data through Windows.

## What the results mean

| Status | Meaning |
|---|---|
| **Recovered** | Every byte was read and the copy on the destination was verified. |
| **Damaged** | Saved, but some parts couldn't be read. Those parts are filled with zeros, and the report lists the exact byte ranges. Many photos, videos and documents still open. Optionally, damaged files can get `[DAMAGED]` added to their names. |
| **Failed** | Not saved. The reason is shown: no readable data, encrypted with Windows EFS, destination full, too large for FAT32, verification mismatch... |
| **Not processed** | The recovery was stopped before reaching this file. |

Deleted files carry extra notes. **"Location estimated"** means FAT and exFAT forget a deleted file's cluster chain, so its data is assumed to be contiguous, as every recovery tool does. **"Space reused"** means newer files were written over its location, so the content is probably overwritten. **"First letter lost"** means FAT replaces the first letter of a deleted short name (`gone.jpg` comes back as `_one.jpg`).

Names Windows can't use are made safe and visible. For example, `what?.txt` becomes `what_.txt` and `CON` becomes `_CON`. Two names that differ only in letter case become `file.txt` and `file (2).txt`, and a deleted copy next to a live one becomes `file (deleted).txt`. The report maps every original name to its saved name.

## Notifications and error codes

Lifeboat makes problems impossible to miss:

* **Pop-ups** (bottom right) for warnings and errors. Errors stay until you close them.
* **A red banner with buttons** whenever Lifeboat needs a decision: the source disconnected, the source stopped responding, or the destination is full or gone. For a disconnected drive it waits and **continues automatically** once the drive is back.
* **Problems tab**: every warning and error with its code, an explanation, and what to do. The badge in the status bar shows the count.
* **Windows notification, sound and taskbar flash** when a job finishes or needs you while the window is in the background (configurable in Settings).
* **Disk map**: a live picture of the drive showing what was read, skipped, failed or found bad.
* **Report** (`Lifeboat Report.html` / `.csv`) in every recovery folder.
* **Log files** in `%LOCALAPPDATA%\Lifeboat\logs` (*Settings → Open log folder*).

| Code | Meaning |
|---|---|
| LB-101 | No access to the drive: run as administrator |
| LB-110 / LB-111 | Unreadable or slow sectors (skipped now, retried later) |
| LB-120 / LB-121 | Source disconnected / not responding (Lifeboat waits and resumes) |
| LB-201...LB-207 | Partition or filesystem problems (missing table, damaged boot sector, unreadable MFT parts, BitLocker, ...) |
| LB-301 | Destination is on the drive being recovered (refused) |
| LB-302 / LB-303 / LB-304 | Not enough space / destination full / destination gone |
| LB-305 | File of 4 GB or more for a FAT32 destination |
| LB-306 / LB-307 | Write error / verification mismatch on the destination |
| LB-401 / LB-402 | File recovered with unreadable parts / not recoverable |
| LB-403 / LB-404 / LB-405 | EFS-encrypted / space reused / unsupported storage format |
| LB-500 | Unexpected internal error (Lifeboat keeps running; please keep the log) |

## What Lifeboat can read

| | |
|---|---|
| Partition tables | MBR (including extended/logical partitions), GPT (with automatic fallback to the backup GPT), drives with no partition table |
| Filesystems | **NTFS** (deleted files and folders, compressed, sparse, alternate data streams, hard links, damaged MFT areas, backup boot sector, $MFTMirr), **exFAT**, **FAT32 / FAT16 / FAT12** (long names, including on deleted entries; second FAT copy used when the first is damaged) |
| Deep scan | Lost and deleted NTFS/exFAT/FAT partitions (also via their backup boot sectors) and files by signature: JPEG, PNG, GIF, BMP, TIFF, Canon CR2/CR3, Nikon NEF, Sony ARW, Olympus ORF, Pentax PEF, DNG, HEIC/AVIF, PSD, MP4, MOV, M4A, 3GP, AVI, WAV, WebP, MKV/WebM, MP3, OGG, PDF, DOCX/XLSX/PPTX, ODT/ODS, EPUB, ZIP, DOC/XLS/PPT/MSG, 7-Zip, RAR, SQLite, Outlook PST |
| Sources | Physical drives, drive letters (volumes), disk images (`.img`, `.dd`, `.raw`, `.bin`, `.iso`, split `.001/.002...`, fixed-size `.vhd`) |

Not supported yet: browsing Mac (HFS+, APFS), Linux (ext2/3/4) and ReFS filesystems. Deep scan still recovers common file types from them by signature. Windows EFS-encrypted files can't be decrypted without the original user's key, and Windows system files compressed with "CompactOS" (WOF) are listed but skipped.

## Command line

`lifeboat-cli.exe` (installed next to `Lifeboat.exe`) runs the same engine for scripts and batch jobs. Run it from an administrator command prompt.

```bat
lifeboat-cli devices
lifeboat-cli scan \\.\PhysicalDrive2 --list
lifeboat-cli recover \\.\PhysicalDrive2 D:\Recovered --include "*.jpg" --include "*.mp4"
lifeboat-cli recover \\.\PhysicalDrive2 D:\Recovered --path "Users/Bob/Documents" --thoroughness maximum
lifeboat-cli recover \\.\PhysicalDrive2 D:\Recovered\"Lifeboat Recovery 2026-10-09 13.48" --resume
lifeboat-cli image \\.\PhysicalDrive2 E:\drive2.img
lifeboat-cli recover E:\drive2.img D:\Recovered --deleted-only
```

Exit codes: `0` everything recovered, `1` finished with damaged or failed files, `2` failed (including no matching files), `3` bad arguments, `130` stopped with Ctrl+C.

## Adding the owner logo

The publisher logo appears in the header, the About box and the installer branding. To add it, save the logo as `lifeboat/assets/owner-logo.png` (or `.svg`, `.jpg`, `.webp`) and push. The next build includes it automatically. Names, colours and the publisher link are set in `lifeboat/branding.py`.

## Building from source

```bash
python -m pip install -e ".[dev]" pillow
python -m lifeboat                      # desktop app
python -m lifeboat scan disk.img        # command line
python -m pytest -q                     # tests (the filesystem-image tests need Linux, see below)
python packaging/make_icon.py           # icon and installer images
pyinstaller packaging/lifeboat.spec --noconfirm
ISCC /DAppVersion=1.0.0 packaging\installer.iss    # Windows installer (Inno Setup 6)
```

The test suite builds real NTFS, FAT12/16/32 and exFAT images with the reference Linux tools (`mkntfs`/`ntfs-3g`, `mkfs.vfat`/`mtools`, `mkfs.exfat`/`exfat-fuse`, `sfdisk`, `sgdisk`). It needs those tools and root for FUSE mounts. CI runs:

* **Linux:** lint (ruff), type checking (mypy), and the full suite. That covers byte-for-byte recovery of live, deleted, fragmented, compressed and sparse files; damaged GPT, boot sectors, MFT and FAT; simulated bad, flaky, slow and hanging sectors; disconnects; a genuinely full destination; stop-and-resume; carving of 20 file types; 200 random-corruption cases; and the GUI.
* **Windows:** the unit tests, then an end-to-end test on a **real NTFS disk** (a VHDX created and formatted by Windows). It recovers every file, including deleted ones, compressed ones and paths longer than 260 characters, through the raw disk, the drive letter, and the packaged `lifeboat-cli.exe`. Then it builds and smoke-tests `Lifeboat.exe`, and packages the installer and portable zip.

## How it works

```
device/    read-only access: Windows raw disks and volumes (overlapped I/O with
           timeouts and cancellation), Linux block devices, image files
rescue/    the sector map (good / skipped / failed / bad, ddrescue compatible) and
           the rescue reader: FAST -> SWEEP -> TRIM -> SCRAPE -> RETRY passes
fs/        MBR/GPT, NTFS, FAT, exFAT, LZNT1 decompression, signature carving
scan/      quick and deep scans, one browsable tree
recover/   planning, safe names, verified copying, rescue passes, journal, reports
imaging/   sector-by-sector imaging with a resumable map
ui/        the desktop app (PySide6 / Qt)
```

Lifeboat never opens a source for writing. All reads go through the rescue reader, which records what it learns in the sector map. Large blocks are read first. After an error, it skips ahead (and skips further after each new error) instead of retrying. Later passes localise errors down to the drive's physical sector size (4 KB on modern drives), so a damaged area costs as few reads as possible. The same map drives the disk-map view, decides each file's status, and is saved with recoveries and images so work can resume.

---

Lifeboat Data Recovery © Zays. Built with Python and Qt for Python (PySide6, LGPL v3).
