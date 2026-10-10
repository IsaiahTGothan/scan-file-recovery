# Known issues and to-do

Found while using Lifeboat on a real recovery: a 5 TB Seagate Portable formatted for Mac (HFS+), a deep scan of about 12 hours, 8,508 files (3.41 TB) found by signature, and a destination with 1.82 TB free. Nothing below has been changed in the software yet.

## Open issues

### 1. No way to continue on another drive when the destination fills up (high)
- **What happens:** the "Destination drive is full" banner only offers **Retry** (after freeing space), **Skip this file** and **Stop**. Skip leaves out a single file, so the next file brings the same banner back.
- **Why it matters:** a recovery bigger than one drive has to be split by hand.
- **Fix:** a **Continue on another drive…** button. It asks for a folder on another drive and carries on there with the remaining files, in the same folder layout, with a report on each drive. Resume should know about both drives. Also replace per-file Skip with **Skip all files that don't fit**.
- **Workaround:** split the selection by type (**All files** dropdown, then **Tick all shown**), about 1.75 TB per drive, and run one recovery per drive from the same scan.

### 2. Scan results are not saved (high)
- **What happens:** results live only in the open window. Closing Lifeboat, installing an update, clicking another drive in Sources, or pressing Scan or Deep scan again throws them away. A deep scan of a 5 TB drive takes about 12 hours.
- **Fix:** save every scan automatically (per drive, in the app data folder) and offer **Reopen last scan of this drive** when that drive is selected. An interrupted deep scan should continue where it stopped instead of starting over.
- **Workaround:** keep the window open until every recovery from that scan is finished.

### 3. No filter for "not recovered yet" (high)
- **What happens:** after a stopped or partial recovery there's no way to tick only the files that didn't make it.
- **Fix:** add **Not recovered yet** to the status filter, based on the last recovery's results, so a second run picks up exactly the rest.

### 4. Mac drives (HFS+) can't be browsed (high for Mac drives)
- **What happens:** HFS+ partitions show as "HFS+, not readable". Only signature search finds files, which loses every name and folder (files come out as `JPG_00012345678.jpg`) and can break fragmented files, large videos especially.
- **Fix:** read HFS+ itself (catalog and extents B-trees) to list live files with names, folders and dates; then deleted files where possible; then APFS.
- **Workaround:** copy the files on a Mac, or with a read-only HFS+ reader on Windows such as HFSExplorer.

### 5. Windows can go to sleep during a long job (medium)
- **What happens:** Lifeboat doesn't keep the PC awake. If Windows sleeps, the job pauses and USB drives can drop off.
- **Fix:** keep the system awake while a job runs (`SetThreadExecutionState`) and release it afterwards.
- **Workaround:** Settings → System → Power → sleep **Never** while plugged in.

### 6. Builds can't be told apart (medium)
- **What happens:** every build says version 1.0.0, so there's no way to see whether an installed copy has the latest fixes.
- **Fix:** show the build number and commit in the About box and window title, and put them in the installer file name.

### 7. The "not enough space" dialog is confusing (low)
- **What happens:** the Recover dialog shows a red "not enough space" error and a green "1.82 TB free" check at the same time. It says "Lifeboat will stop and ask you when the drive is full", but **Start recovery** stays disabled until **Recover as much as fits** is ticked.
- **Fix:** show one clear message that says how much fits, and offer to split the selection across drives (see 1).

### 8. Verification doesn't overlap with copying (low, speed)
- **What happens:** each file is copied and then read back before the next file starts. On hard-drive destinations this adds a lot of time.
- **Fix:** verify the previous file while the next one is being copied.

### 9. Deep scan and imaging each read the whole drive (idea)
- **Fix:** an option to write a disk image during the deep scan, so a weak drive is read only once.

## Limitations (not supported yet)
- Owner logo from zays.us: the build environment's network policy blocks the site. Add the logo as `lifeboat/assets/owner-logo.png`.
- Not code-signed: Windows SmartScreen warns on first run.
- Not yet tested on a physically failing drive. Tested only with simulated failures and healthy real disks.
- Can't browse APFS, ext2/3/4 or ReFS (signature search still works on them).
- Windows EFS-encrypted files can't be decrypted. Windows system files compressed with CompactOS (WOF) are skipped.
- Physical damage (clicking, not spinning up, not detected) needs a clean-room lab.

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
