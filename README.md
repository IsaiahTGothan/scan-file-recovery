<p align="center">
  <img src="lifeboat/assets/lifeboat.png" width="96" alt="Lifeboat icon">
</p>

<h1 align="center">Lifeboat Data Recovery</h1>
<p align="center"><b>Get your files off a failing drive.</b><br>
Read-only, bad-sector aware, and it tells you about every problem.<br>
developed by ZAYS</p>

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

Every push to this repository builds and tests Lifeboat on Windows and on Macs (Apple Silicon and Intel).

### Windows

1. Open the repository's **Actions** tab, then the latest successful **Build and test** run.
2. Under **Artifacts**, download **Lifeboat-Windows**. It contains:
   * `Lifeboat-Setup-1.2.0-build19.exe`: the installer (Start menu entry, optional desktop icon, uninstaller).
   * `Lifeboat-1.2.0-build19-portable.zip`: the same app with no installation. Unzip it and run `Lifeboat.exe`.

   The build number (19 here) is the number of the Actions run. The About box shows it too, so you can always tell which build is installed.

Pushing a version tag (for example `v1.2.0`) also publishes both files as a GitHub Release.

Lifeboat asks for **administrator rights** when it starts (the UAC prompt). Windows only allows direct disk access to administrators. Because the app isn't code-signed yet, SmartScreen may say "Windows protected your PC". Click **More info → Run anyway**.

Requirements: Windows 10 or 11, 64-bit.

### Mac

1. In the same **Build and test** run, download **Lifeboat-macOS-AppleSilicon** for a Mac with an Apple chip (M1 or later: Apple menu → **About This Mac** shows "Chip: Apple M…"), or **Lifeboat-macOS-Intel** for an Intel Mac.
2. Open the `.dmg` and drag **Lifeboat** to Applications, or anywhere else: the app is self-contained, so it also runs straight from a USB stick (the `.zip` holds the same app).
3. **First start:** the app isn't notarized by Apple yet, so macOS blocks it the first time. Open **System Settings → Privacy & Security**, scroll down to the message about Lifeboat and click **Open Anyway**. (If macOS says the app "is damaged", run `xattr -dr com.apple.quarantine /Applications/Lifeboat.app` in Terminal, then open it again.)
4. Lifeboat offers to **restart with administrator rights**: macOS asks for your password. Only administrators may read drives directly, on a Mac too. If macOS still refuses to let Lifeboat read a drive, allow Lifeboat under **System Settings → Privacy & Security → Full Disk Access**.

Requirements: macOS 14 (Sonoma) or later.

On a Mac, Lifeboat lists every physical drive (and attached disk images) with its name, size and connection, reads through the raw device (`/dev/rdiskN`), and has the same Drive check. Mac drives (HFS+ and APFS, including Time Machine disks) are browsed with their real names and folders on both Mac and Windows, encrypted APFS volumes too once you give their password. The command line tool is inside the app: `sudo /Applications/Lifeboat.app/Contents/MacOS/lifeboat-cli devices`.

Mac tips: if macOS says "The disk you attached was not readable by this computer", click **Ignore**, never **Initialize**, and don't run **First Aid** on a failing drive (both write to it). Spotlight starts indexing every drive you connect; on a failing drive that's a lot of extra reading, so add its volume under **System Settings → Spotlight → Search Privacy**, or keep it unmounted.

## Recovering files: the 4 steps

1. **Pick the drive.** Connect the failing drive and select it under **Sources**. The overview shows its model, capacity, sector size, connection and, when Windows can read it, its SMART health.
2. **Scan it.** Click **Scan**. Lifeboat reads the partition table and the file tables (NTFS MFT, FAT, exFAT) and rebuilds the folders, including deleted files and folders. If partitions are missing or the drive was formatted, use **Deep scan**: it reads the whole drive to find lost partitions and to recognise files by their content.
3. **Tick your files.** Browse folders, search (`*.jpg`, `invoice`), filter by type with one click (the chips above the list show how many photos, videos, documents... were found) or by existing/deleted, and press **Preview** to look inside a file first. The **Chance** column estimates how recoverable each file is. The arrow next to **Tick all shown** has more ways to tick:
   * **Tick the first half / second half**: split a recovery that is too big for one drive. The halves are split by size, in disk order, so each one is one stretch of the source drive.
   * **Tick up to a size…** and **Tick what fits on a drive…** (pick a folder on the destination; Lifeboat fills its free space in disk order).
   * **Tick files not recovered yet** and **Untick files already recovered**: every recovery since the scan counts.
   * **Invert ticks**, **Untick all**.
4. **Recover.** Click **Recover N files**, choose a folder on a different drive, and pick how hard to try:
   * **Quick**: copy what reads easily and never retry.
   * **Standard** (recommended): then go back for the skipped and damaged areas, sector by sector.
   * **Maximum**: Standard, plus three more tries on every bad sector.

   Before anything is written, Lifeboat checks the destination. It refuses a folder on the drive being recovered, warns about FAT32's 4 GB file limit, and when space is short offers **Tick only what fits here**. Each recovery gets its own dated folder containing your files, `Lifeboat Report.html` and `Lifeboat Report.csv`.

   **More files than one drive holds?** Use **Tick only what fits here** (or Stop when the drive is full). When the recovery is done, the summary offers **Recover the rest…**: it ticks exactly the files not recovered yet, and you pick another drive.

While a job runs, the panel at the top shows how far it is, the reading speed (with a graph of the last two minutes), the time left and how much couldn't be read. The **Disk map** tab shows every part of the drive: blocks light up as they're read, the block being read glows, and pointing at a block tells you what it holds. You can **Pause**, **Stop** (everything recovered so far is kept) or **Finish now** (skip the remaining retry passes) at any time. To continue a stopped recovery, start a recovery into the same folder and tick **Resume**. Files that were already verified are skipped. Lifeboat keeps the computer from sleeping while a job runs.

**Settings** (the gear at the top right, or Ctrl+,) has a search box and these pages:

* **Appearance:** theme (dark, light or the system's), accent color, text size, row spacing, the disk map animation (Smooth, Light or Off), file sizes as Windows counts them (1 KB = 1,024 bytes) or as macOS does (1,000), and the date format. Reports always use year-month-day.
* **Scanning:** what a scan shows first (existing and deleted files, or one kind), system files, how long to re-read damaged file tables, and what Deep scan looks for by default (it remembers your last choice).
* **Notifications, Recovery, Reading drives:** sounds, notifications and pop-ups; the Recover window's defaults and whether to open the folder or the report when it's done; how long to wait for slow or sleeping drives.
* **System:** keeping the computer awake, offering administrator rights at start, how much goes into the log, and the log folder. Language and automatic updates are coming soon.
* **Account & license** and **Keyboard shortcuts.**

**Plans.** Lifeboat is in early access: every feature is unlocked for free until launch, shown by the **EARLY ACCESS** badge at the top. **Upgrade** shows the plans to come (Free, Pro, Technician) and the free trial; their buttons only say "coming soon" for now. Nothing asks for payment details. The plans, prices and features live in one file, `lifeboat/plans.py`.

## When the drive is really failing

* **If Windows offers to format the drive or "scan and fix" it, click Cancel.** Both write to the drive and can destroy what's left.
* **Clicking, beeping, very slow, or keeps disconnecting? Make a disk image first.** **Create image** copies the whole drive to a file on a healthy drive (it needs as much free space as the failing drive's size). The healthy areas are copied first. The progress map (`.map`, compatible with GNU ddrescue) means you can stop and resume. Then open the image with **Open image** and recover from it without touching the failing drive again. Files recovered from the image are still marked Damaged wherever the drive couldn't be read.
  The map records which drive the image belongs to. Lifeboat continues an image only for that drive and never overwrites an existing file that has no map (LB-309). If you moved the same drive to another USB adapter and it now reports a different serial number, tick **It is this same drive** (command line: `--same-drive`) to continue.
* **Keep File Explorer, antivirus scans and backup software away from the failing drive** while Lifeboat works. They compete for the same weak heads.
* **USB adapters matter.** If a drive keeps dropping off or shows the wrong size, try another cable, port or adapter. Lifeboat detects when an enclosure used 4 KB sectors and the dock shows 512-byte sectors (or the reverse) and adjusts automatically.
* **Drive in Device Manager but not in Disk Management?** Click **Drive check** (top of *Sources*). It lists every storage device Windows detects, says for each one whether Disk Management can use it and whether its first sector reads, and explains what to try next: another cable, port, power supply or adapter, or a lab. A drive that is detected but doesn't report its size also stays in *Sources*, marked **CHECK**, instead of disappearing. The check only reads (one small read per drive, with a time limit) and leaves alone the drive a job is reading. **Copy report** puts the results on the clipboard, e.g. for a ticket.
* **A drive that doesn't spin up or isn't detected at all** has a hardware fault (PCB, heads, motor). No software can read it; it needs a clean-room lab.
* **BitLocker drives:** unlock the drive in Windows (password or recovery key), then select its **drive letter** under *Volumes*. Lifeboat then reads the decrypted data through Windows.
* **Encrypted Mac drives** (FileVault, "APFS (Encrypted)", encrypted Time Machine backups) show up after a scan with a lock. Select the volume, click **Unlock** and type the password used on the Mac (the disk's password, a user's login password for FileVault, or the recovery key). Lifeboat decrypts it itself, on Windows too, without writing anything; the password stays in memory until Lifeboat closes. On the command line: `--password` (or the environment variable `LIFEBOAT_PASSWORD`, which keeps it out of the shell history).

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

* **Pop-ups** (bottom right) for warnings and errors. Errors stay until you close them. When problems arrive in a burst (a dying drive can fail thousands of files), they're summed up in one "N more problems" pop-up instead of flooding the screen.
* **A red banner with buttons** whenever Lifeboat needs a decision: the source disconnected, the source stopped responding, or the destination is full or gone. For a disconnected drive it waits and **continues automatically** once the drive is back. Pressing Retry too early is harmless: it just keeps waiting.
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
| LB-308 / LB-309 | Destination folder not writable / an image file of that name already exists (never overwritten) |
| LB-401 / LB-402 | File recovered with unreadable parts / not recoverable |
| LB-403 / LB-404 / LB-405 | EFS-encrypted / space reused / unsupported storage format |
| LB-500 | Unexpected internal error (Lifeboat keeps running; please keep the log) |

## What Lifeboat can read

| | |
|---|---|
| Partition tables | MBR (including extended/logical partitions), GPT (with automatic fallback to the backup GPT), Apple Partition Map, drives with no partition table |
| Filesystems | **NTFS** (deleted files and folders, compressed, sparse, alternate data streams, hard links, damaged MFT areas, backup boot sector, $MFTMirr), **exFAT**, **FAT32 / FAT16 / FAT12** (long names, including on deleted entries; second FAT copy used when the first is damaged), **Mac HFS+** (Mac OS Extended, journaled and case-sensitive too: deleted files and folders from the journal and unused catalog nodes, files macOS compressed, hard links, Time Machine folder links, resource forks, unfinished journal changes applied in memory, backup volume header), **Mac APFS** (every volume of the container, encrypted volumes unlocked with their password, compressed, sparse and cloned files, hard links, snapshots, recently deleted files from older checkpoints, newest valid checkpoint when the superblock is damaged) |
| Deep scan | Lost and deleted NTFS/exFAT/FAT/HFS+ partitions (also via their backup boot sectors and volume headers), deleted APFS files with their names and folders (from the old file-tree pages APFS leaves in free space), and files by signature: JPEG, PNG, GIF, BMP, TIFF, Canon CR2/CR3, Nikon NEF, Sony ARW, Olympus ORF, Pentax PEF, DNG, HEIC/AVIF, PSD, MP4, MOV, M4A, 3GP, AVI, WAV, WebP, MKV/WebM, MP3, OGG, PDF, DOCX/XLSX/PPTX, ODT/ODS, EPUB, ZIP, DOC/XLS/PPT/MSG, 7-Zip, RAR, SQLite, Outlook PST |
| Sources | Physical drives, drive letters (volumes), disk images (`.img`, `.dd`, `.raw`, `.bin`, `.iso`, split `.001/.002...`, fixed-size `.vhd`) |

Not supported yet: Linux (ext2/3/4) and ReFS filesystems (deep scan still recovers common file types from them by signature). A Mac's internal SSD (T2 and Apple silicon) is encrypted by the Mac's own chip and can't be read anywhere else. Windows EFS-encrypted files can't be decrypted without the original user's key, and Windows system files compressed with "CompactOS" (WOF) are listed but skipped.

## Command line

`lifeboat-cli.exe` (installed next to `Lifeboat.exe`) runs the same engine for scripts and batch jobs. Run it from an administrator command prompt. On a Mac it is `Lifeboat.app/Contents/MacOS/lifeboat-cli`, run with `sudo`, and drives are named `/dev/rdisk4` instead of `\\.\PhysicalDrive2`.

```bat
lifeboat-cli devices
lifeboat-cli check
lifeboat-cli scan \\.\PhysicalDrive2 --list
lifeboat-cli recover \\.\PhysicalDrive2 D:\Recovered --include "*.jpg" --include "*.mp4"
lifeboat-cli recover \\.\PhysicalDrive2 D:\Recovered --path "Users/Bob/Documents" --thoroughness maximum
lifeboat-cli recover \\.\PhysicalDrive2 D:\Recovered\"Lifeboat Recovery 2026-10-09 13.48" --resume
lifeboat-cli image \\.\PhysicalDrive2 E:\drive2.img
lifeboat-cli recover E:\drive2.img D:\Recovered --deleted-only
lifeboat-cli recover \\.\PhysicalDrive3 D:\Recovered --password "the Mac password"
lifeboat-cli selftest
```

Exit codes: `0` everything recovered, `1` finished with damaged or failed files, `2` failed (including no matching files), `3` bad arguments, `130` stopped with Ctrl+C. `lifeboat-cli check` (the Drive check, `--json` for scripts) exits with `1` when a device needs attention.

## Adding the owner logo

The publisher logo appears in the header, the About box and the installer branding. To add it, save the logo as `lifeboat/assets/owner-logo.png` (or `.svg`, `.jpg`, `.webp`) and push. The next build includes it automatically. Names, colours and the publisher link are set in `lifeboat/branding.py`.

## Building from source

```bash
python -m pip install -e ".[dev]" pillow
python -m lifeboat                      # desktop app
python -m lifeboat scan disk.img        # command line
python -m pytest -q                     # tests (the filesystem-image tests need Linux, see below)
python packaging/stamp_build.py         # optional: build number and commit for the About box
python packaging/make_icon.py           # icon and installer images
pyinstaller packaging/lifeboat.spec --noconfirm       # Windows: Lifeboat.exe + lifeboat-cli.exe
ISCC /DAppVersion=1.2.0 packaging\installer.iss    # Windows installer (Inno Setup 6)
pyinstaller packaging/lifeboat_mac.spec --noconfirm   # Mac: Lifeboat.app (with lifeboat-cli inside)
```

Mac filesystems are tested on real disks made by macOS: a workflow (`.github/workflows/apple-fixtures.yml`) runs `tests/macos/make_fixtures.py` on a Mac whenever that script changes and commits the gzipped disks to `tests/fixtures/apple`. They hold compressed files, hard links, a Time Machine-style folder link, a file in dozens of pieces, a sparse file, accented, Japanese and emoji names, and deleted files and folders.

The test suite builds real NTFS, FAT12/16/32 and exFAT images with the reference Linux tools (`mkntfs`/`ntfs-3g`, `mkfs.vfat`/`mtools`, `mkfs.exfat`/`exfat-fuse`, `sfdisk`, `sgdisk`). It needs those tools and root for FUSE mounts. CI runs:

* **Linux:** lint (ruff), type checking (mypy), and the full suite. That covers byte-for-byte recovery of live, deleted, fragmented, compressed and sparse files; damaged GPT, boot sectors, MFT and FAT; simulated bad, flaky, slow and hanging sectors; disconnects; a genuinely full destination; stop-and-resume; carving of 20 file types; 200 random-corruption cases; and the GUI.
* **Mac (Apple Silicon and Intel):** the unit tests, then **real FAT32, exFAT, HFS+ and APFS disks made by macOS** (`hdiutil`, files written and deleted through macOS's own drivers), attached read-only and recovered byte for byte through the raw device, by the Python package and by the `lifeboat-cli` inside the app. Then it builds `Lifeboat.app`, smoke-tests it, and packages the `.dmg` and `.zip`.
* **Windows:** the unit tests, then an end-to-end test on a **real NTFS disk** (a VHDX created and formatted by Windows). It recovers every file, including deleted ones, compressed ones and paths longer than 260 characters, through the raw disk, the drive letter, and the packaged `lifeboat-cli.exe`. Then it builds and smoke-tests `Lifeboat.exe`, and packages the installer and portable zip.

## How it works

```
device/    read-only access: Windows raw disks and volumes (overlapped I/O with
           timeouts and cancellation), Linux block devices, image files
rescue/    the sector map (good / skipped / failed / bad, ddrescue compatible) and
           the rescue reader: FAST -> SWEEP -> TRIM -> SCRAPE -> RETRY passes
fs/        MBR/GPT/Apple Partition Map, NTFS, FAT, exFAT, HFS+, APFS, LZNT1 and
           Apple (zlib/LZVN/LZFSE) decompression, signature carving
scan/      quick and deep scans, one browsable tree
recover/   planning, safe names, verified copying, rescue passes, journal, reports
imaging/   sector-by-sector imaging with a resumable map
ui/        the desktop app (PySide6 / Qt)
```

Lifeboat never opens a source for writing. All reads go through the rescue reader, which records what it learns in the sector map. Large blocks are read first. After an error, it skips ahead (and skips further after each new error) instead of retrying. Later passes localise errors down to the drive's physical sector size (4 KB on modern drives), so a damaged area costs as few reads as possible. The same map drives the disk-map view, decides each file's status, and is saved with recoveries and images so work can resume.

---

Lifeboat Data Recovery © ZAYS. Built with Python and Qt for Python (PySide6, LGPL v3).
