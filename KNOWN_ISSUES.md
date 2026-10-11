# Known issues and to-do

Found while using Lifeboat on a real recovery: a 5 TB Seagate Portable formatted for Mac (HFS+), a deep scan of about 12 hours, 8,508 files (3.41 TB) found by signature, and a destination with 1.82 TB free. The open issues below haven't been changed in the software yet; the fixed ones are listed at the end.

## Open issues

### 2. Scan results are not saved (high)
- **What happens:** results live only in the open window. Closing Lifeboat, installing an update, clicking another drive in Sources, pressing Scan or Deep scan again, or the computer crashing (it happened during the Seagate recovery) throws them away. A deep scan of a 5 TB drive takes about 12 hours.
- **Fix:** save every scan automatically (per drive, in the app data folder) and offer **Reopen last scan of this drive** when that drive is selected. An interrupted deep scan should continue where it stopped instead of starting over.
- **Workaround:** keep the window open until every recovery from that scan is finished.

### 4. Mac drives (HFS+) can't be browsed (high for Mac drives)
- **What happens:** HFS+ partitions show as "HFS+, not readable". Only signature search finds files, which loses every name and folder (files come out as `JPG_00012345678.jpg`) and can break fragmented files, large videos especially.
- **Fix:** read HFS+ itself (catalog and extents B-trees) to list live files with names, folders and dates; then deleted files where possible; then APFS.
- **Workaround:** copy the files on a Mac, or with a read-only HFS+ reader on Windows such as HFSExplorer.

### 8. Verification doesn't overlap with copying (low, speed)
- **What happens:** each file is copied and then read back before the next file starts. On hard-drive destinations this adds a lot of time.
- **Fix:** verify the previous file while the next one is being copied.

### 9. Deep scan and imaging each read the whole drive (idea)
- **Fix:** an option to write a disk image during the deep scan, so a weak drive is read only once.

### 10. A computer crash leaves no report (medium)
- **What happens:** the HTML and CSV reports are written when the recovery ends. When Windows crashed during the Seagate recovery, the folder kept the recovered files but had no list of what was done, apart from Lifeboat's internal journal.
- **Fix:** update the CSV report as files finish (every few seconds), and write the HTML report at the end.

### 11. Resuming after a new scan depends on finding exactly the same files (medium)
- **What happens:** files found by signature sit in a folder named "Files found by signature (8,508)", and that count is part of each file's identity. If a new deep scan finds 8,507 or 8,509 files, Resume recognises none of the files already recovered. It copies everything again, into a new folder.
- **Fix:** identify files found by signature by their position on the drive and their size, not by the folder name.

### 12. Signature search could save files while it scans (idea, speed)
- **What happens:** a deep scan reads the whole drive (12 hours here), then the recovery reads the same data again.
- **Fix:** an option to save each file found by signature as soon as it is found, so one pass over the drive both finds and recovers the files.

## Limitations (not supported yet)
- Owner logo from zays.us: the build environment's network policy blocks the site. Add the logo as `lifeboat/assets/owner-logo.png`.
- Not code-signed: Windows SmartScreen warns on first run. The Mac app isn't notarized by Apple: macOS blocks the first start until you click **Open Anyway** (System Settings → Privacy & Security).
- Not yet tested on a physically failing drive. Tested only with simulated failures and healthy real disks.
- The Mac version is tested on GitHub's Macs (Apple Silicon and Intel) with disk images that macOS itself formats and attaches as disks, not yet on a person's Mac with a USB drive. Its drive list doesn't read serial numbers yet, so a reconnected drive is recognised by its name and size.
- Can't browse APFS, ext2/3/4 or ReFS (signature search still works on them).
- Windows EFS-encrypted files can't be decrypted. Windows system files compressed with CompactOS (WOF) are skipped.
- Physical damage (clicking, not spinning up, not detected) needs a clean-room lab.

## Fixed on October 11, 2026 (version 1.1.0)
- **1. No way to continue on another drive when the destination fills up.** The Recover window now offers **Tick only what fits here** when the destination is too small: it keeps ticked, in disk order, the files that fit, and opens the Recover window again. When the recovery ends (or is stopped because the drive filled up), the summary offers **Recover the rest…**, which ticks exactly the files not recovered yet so they can go to another drive. A running recovery still can't switch drives by itself: press Stop, then Recover the rest.
- **3. No filter for "not recovered yet".** The arrow next to **Tick all shown** has **Tick files not recovered yet** and **Untick files already recovered**, which count every recovery since the scan, plus **Tick the first half / second half** (by size, in disk order), **Tick up to a size…**, **Tick what fits on a drive…** and **Invert ticks**. Lifeboat remembers what was recovered only while it stays open, because scans aren't saved yet (issue 2).
- **5. Windows could go to sleep during a long job.** Lifeboat now keeps the computer awake while a scan, recovery or image runs (Settings → System, on by default). The screen can still turn off.
- **6. Builds couldn't be told apart.** The version is now 1.1.0, and the About box, `lifeboat-cli --version`, the log and the reports show the build number and commit, for example "1.1.0 (build 19, a8fa2e7)". The installer and zip file names include the build number.
- **7. The "not enough space" dialog was confusing.** It now shows one message (what the files need, what's free) with **Tick only what fits here**, or **Start anyway** (Lifeboat stops and asks when the drive is full).
- **13. Drives that Windows detects but can't read were hidden.** The drive list skipped any drive that didn't report its size, which is exactly what a failing drive does when it shows in Device Manager but not in Disk Management. Now such drives stay in Sources, marked **CHECK** ("Not answering", or "No media" for an empty card reader), and the new **Drive check** lists every storage device Device Manager shows (disk drives, USB storage adapters with no drive behind them, and devices with errors such as Code 43 or Code 10). For each one it says whether Disk Management can use it and whether its first sector reads, and what to try next. Also on the command line: `lifeboat-cli check`.
- A drive waking up from sleep was reported as "The source drive disconnected" (red error and pop-up), although it answered again two seconds later. Now a drive that stops answering gets up to 30 seconds to come back. Lifeboat notes the wait in the Activity log, reads again, and reports a disconnect only if the drive is still gone after that.

## Fixed in the build from commit fb50af8 (October 9, 2026)
Builds older than this one still have these problems.
- A disk image made after a scan was missing the partition table and file tables but reported "100% rescued".
- A leftover progress file (.map) without its image, or one from another drive, produced an empty or mixed image.
- The app crashed when quitting after the Recover dialog had been used.
- The app crashed when another drive was clicked, or the drive list refreshed, while a job was running.
- The **Resume** checkbox in the Recover dialog never worked.
- Closing a pop-up before it timed out showed a false "Unexpected error".
- Pressing Retry on the "drive disconnected" banner before the drive was back cancelled the job.
- Adding "[DAMAGED]" to a file name could overwrite another recovered file, and long names could fail to save.
- `lifeboat-cli --resume` into a folder from a different drive reused that drive's data.
- A disconnect while Lifeboat was locating a file on a FAT32 drive marked the file as lost.
- The "don't write to the drive being recovered" check could pass silently, and folder mount points weren't resolved.
- Text from the drive could turn into spreadsheet formulas in the CSV report.
