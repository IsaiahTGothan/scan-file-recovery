"""Find files by signature ("file carving") when no filesystem is left.

Each supported format has a strict header check and a *measurer* that
parses the file's own structure to find where it ends.  Only files whose
structure checks out are reported, which keeps false positives low.

Files start on sector boundaries in every filesystem, so only the first
bytes of each 512-byte sector are examined.  Once a file is found, its
whole extent is skipped (that is how embedded thumbnails inside photos or
pictures inside documents are not reported twice).
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass

from ..rescue.reader import ReadMode, RescueReader
from .model import Extent, F, FileLayout, Node, Volume

MiB = 1 << 20
SECTOR = 512

Fetch = Callable[[int, int], bytes]  # (offset relative to file start, length) -> bytes


@dataclass
class Found:
    size: int
    ext: str
    kind: str          # group name shown in the tree
    exact: bool = True  # size derived from the file's own structure


# ----------------------------------------------------------------------- formats
def _jpeg(fetch: Fetch) -> Found | None:
    head = fetch(0, 4)
    if head[:3] != b"\xff\xd8\xff" or head[3] < 0xC0:
        return None
    pos = 2
    max_size = 128 * MiB
    # Marker segments until Start Of Scan.
    while pos < max_size:
        seg = fetch(pos, 4)
        if len(seg) < 4 or seg[0] != 0xFF:
            return None
        marker = seg[1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0xD9:
            return Found(pos + 2, "jpg", "Photos (JPEG)")
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            pos += 2
            continue
        length = (seg[2] << 8) | seg[3]
        if length < 2:
            return None
        pos += 2 + length
        if marker == 0xDA:
            break
    # Entropy-coded data: byte stuffing guarantees FF D9 only appears as the
    # End Of Image marker (progressive JPEGs have more segments in between).
    window = 1 << 18
    while pos < max_size:
        block = fetch(pos, window)
        if len(block) < 2:
            return None
        hit = block.find(b"\xff\xd9")
        if hit >= 0:
            return Found(pos + hit + 2, "jpg", "Photos (JPEG)")
        if len(block) < window:
            return None
        pos += window - 1
    return None


def _png(fetch: Fetch) -> Found | None:
    if fetch(0, 8) != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    first = True
    while pos < 512 * MiB:
        hdr = fetch(pos, 8)
        if len(hdr) < 8:
            return None
        length = struct.unpack(">I", hdr[:4])[0]
        ctype = hdr[4:8]
        if length > 0x7FFFFFFF or not ctype.isalpha():
            return None
        if first and ctype != b"IHDR":
            return None
        first = False
        pos += 12 + length
        if ctype == b"IEND":
            return Found(pos, "png", "Pictures (PNG)")
    return None


def _gif(fetch: Fetch) -> Found | None:
    head = fetch(0, 13)
    if head[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    flags = head[10]
    pos = 13
    if flags & 0x80:
        pos += 3 * (2 << (flags & 7))

    def skip_subblocks(p: int) -> int | None:
        while True:
            size = fetch(p, 1)
            if not size:
                return None
            p += 1
            if size[0] == 0:
                return p
            p += size[0]

    while pos < 64 * MiB:
        tag = fetch(pos, 1)
        if not tag:
            return None
        if tag[0] == 0x3B:
            return Found(pos + 1, "gif", "Pictures (GIF)")
        if tag[0] == 0x21:
            nxt = skip_subblocks(pos + 2)
        elif tag[0] == 0x2C:
            desc = fetch(pos, 10)
            if len(desc) < 10:
                return None
            p = pos + 10
            if desc[9] & 0x80:
                p += 3 * (2 << (desc[9] & 7))
            nxt = skip_subblocks(p + 1)
        else:
            return None
        if nxt is None:
            return None
        pos = nxt
    return None


def _bmp(fetch: Fetch) -> Found | None:
    head = fetch(0, 54)
    if head[:2] != b"BM" or len(head) < 54:
        return None
    size, reserved, offset, hsize, width, height, planes, bpp = struct.unpack_from("<IIIIiiHH", head, 2)
    if reserved != 0 or hsize not in (12, 40, 52, 56, 108, 124) or planes != 1:
        return None
    if bpp not in (1, 4, 8, 16, 24, 32) or width <= 0 or height == 0 or width > 100000 or abs(height) > 100000:
        return None
    if not 26 <= offset < size or size > 512 * MiB:
        return None
    return Found(size, "bmp", "Pictures (BMP)")


def _iso_bmff(fetch: Fetch) -> Found | None:
    head = fetch(0, 16)
    if head[4:8] != b"ftyp":
        return None
    size = struct.unpack(">I", head[:4])[0]
    if not 16 <= size <= 512:
        return None
    brand = head[8:12]
    known = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"meta", b"pnot", b"PICT",
             b"udta", b"pdin", b"moof", b"mfra", b"sidx", b"styp", b"emsg", b"prft", b"mvex", b"junk",
             b"Xtra", b"ssix", b"idat", b"iinf", b"iloc", b"iprp", b"pitm", b"iref", b"hdlr", b"dinf"}
    pos = 0
    saw_media = False
    while pos < 64 * 1024 * MiB:
        hdr = fetch(pos, 16)
        if len(hdr) < 8:
            break
        box = struct.unpack(">I", hdr[:4])[0]
        btype = hdr[4:8]
        if btype not in known:
            break
        if box == 1:
            box = struct.unpack(">Q", hdr[8:16])[0]
        elif box == 0:
            break  # "to end of file": cannot be measured
        if box < 8:
            break
        if btype in (b"mdat", b"moov", b"meta", b"moof"):
            saw_media = True
        pos += box
    if not saw_media or pos <= size:
        return None
    if brand in (b"qt  ",):
        ext, kind = "mov", "Videos (MOV)"
    elif brand in (b"heic", b"heix", b"mif1", b"msf1", b"heim", b"heis", b"hevc", b"avif"):
        ext, kind = ("avif", "Pictures (AVIF)") if brand == b"avif" else ("heic", "Photos (HEIC)")
    elif brand.startswith(b"M4A") or brand.startswith(b"M4B"):
        ext, kind = "m4a", "Audio (M4A)"
    elif brand.startswith(b"3g"):
        ext, kind = "3gp", "Videos (3GP)"
    elif brand == b"crx ":
        ext, kind = "cr3", "Camera RAW (CR3)"
    else:
        ext, kind = "mp4", "Videos (MP4)"
    return Found(pos, ext, kind)


def _riff(fetch: Fetch) -> Found | None:
    head = fetch(0, 12)
    if head[:4] != b"RIFF":
        return None
    size = struct.unpack("<I", head[4:8])[0]
    form = head[8:12]
    kinds = {b"AVI ": ("avi", "Videos (AVI)"), b"WAVE": ("wav", "Audio (WAV)"), b"WEBP": ("webp", "Pictures (WebP)")}
    if form not in kinds or size < 4:
        return None
    total = 8 + size + (size & 1)
    if form == b"AVI ":
        # AVI 2.0 files continue with RIFF AVIX chunks.
        while True:
            nxt = fetch(total, 12)
            if len(nxt) < 12 or nxt[:4] != b"RIFF" or nxt[8:12] != b"AVIX":
                break
            extra = struct.unpack("<I", nxt[4:8])[0]
            total += 8 + extra + (extra & 1)
    ext, kind = kinds[form]
    return Found(total, ext, kind)


def _vint(data: bytes, pos: int) -> tuple[int, int, bool] | None:
    if pos >= len(data):
        return None
    first = data[pos]
    length = 1
    mask = 0x80
    while length <= 8 and not first & mask:
        mask >>= 1
        length += 1
    if length > 8 or pos + length > len(data):
        return None
    value = first & (mask - 1)
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    unknown = value == (1 << (7 * length)) - 1
    return value, length, unknown


def _ebml(fetch: Fetch) -> Found | None:
    head = fetch(0, 64)
    if head[:4] != b"\x1a\x45\xdf\xa3":
        return None
    parsed = _vint(head, 4)
    if parsed is None:
        return None
    hsize, hlen, _unknown = parsed
    body = fetch(4 + hlen, hsize)
    doctype = b"webm" if b"webm" in body else b"matroska" if b"matroska" in body else b""
    if not doctype:
        return None
    seg_pos = 4 + hlen + hsize
    seg = fetch(seg_pos, 12)
    if seg[:4] != b"\x18\x53\x80\x67":
        return None
    parsed = _vint(seg, 4)
    if parsed is None:
        return None
    ssize, slen, unknown = parsed
    if unknown:
        return None
    total = seg_pos + 4 + slen + ssize
    return Found(total, "webm" if doctype == b"webm" else "mkv",
                 "Videos (WebM)" if doctype == b"webm" else "Videos (MKV)")


_MP3_BITRATES = {
    1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],   # MPEG1 layer III
    2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],       # MPEG2/2.5 layer III
}
_MP3_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _mp3(fetch: Fetch) -> Found | None:
    head = fetch(0, 10)
    if head[:3] != b"ID3" or head[3] not in (2, 3, 4) or any(b & 0x80 for b in head[6:10]):
        return None
    tag = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
    pos = 10 + tag + (10 if head[5] & 0x10 else 0)
    frames = 0
    while pos < 2 * 1024 * MiB:
        hdr = fetch(pos, 4)
        if len(hdr) < 4 or hdr[0] != 0xFF or (hdr[1] & 0xE0) != 0xE0:
            break
        version = (hdr[1] >> 3) & 3
        layer = (hdr[1] >> 1) & 3
        bitrate_index = hdr[2] >> 4
        rate_index = (hdr[2] >> 2) & 3
        padding = (hdr[2] >> 1) & 1
        if layer != 1 or version == 1 or bitrate_index in (0, 15) or rate_index == 3:
            break
        rate = _MP3_RATES[version][rate_index]
        if version == 3:
            length = 144000 * _MP3_BITRATES[1][bitrate_index] // rate + padding
        else:
            length = 72000 * _MP3_BITRATES[2][bitrate_index] // rate + padding
        if length < 24:
            break
        pos += length
        frames += 1
    if frames < 3:
        return None
    if fetch(pos, 3) == b"TAG":
        pos += 128
    return Found(pos, "mp3", "Audio (MP3)")


def _ogg(fetch: Fetch) -> Found | None:
    if fetch(0, 4) != b"OggS":
        return None
    pos = 0
    pages = 0
    while pos < 2 * 1024 * MiB:
        hdr = fetch(pos, 27)
        if len(hdr) < 27 or hdr[:4] != b"OggS" or hdr[4] != 0:
            break
        nseg = hdr[26]
        table = fetch(pos + 27, nseg)
        if len(table) < nseg:
            break
        pos += 27 + nseg + sum(table)
        pages += 1
        if hdr[5] & 0x04 and pages > 1:
            nxt = fetch(pos, 4)
            if nxt != b"OggS":
                break
    if pages < 2:
        return None
    return Found(pos, "ogg", "Audio (OGG)")


def _pdf(fetch: Fetch) -> Found | None:
    head = fetch(0, 8)
    if head[:5] != b"%PDF-" or head[5:6] not in (b"1", b"2"):
        return None
    window = 1 << 20
    pos = 0
    last_end: int | None = None
    while pos < 1024 * MiB:
        block = fetch(pos, window)
        if not block:
            break
        index = 0
        stop = False
        while True:
            eof = block.find(b"%%EOF", index)
            if eof < 0:
                break
            end = pos + eof + 5
            tail = block[eof + 5:eof + 7]
            if tail.startswith(b"\r\n"):
                end += 2
            elif tail[:1] in (b"\r", b"\n"):
                end += 1
            last_end = end
            index = eof + 5
        if last_end is not None:
            # Another PDF starting on a sector boundary after the last %%EOF is a new file.
            header = block.find(b"%PDF-", max(0, last_end - pos))
            while header >= 0:
                if (pos + header) % SECTOR == 0 and pos + header >= last_end:
                    stop = True
                    break
                header = block.find(b"%PDF-", header + 1)
            if pos + len(block) - last_end > 4 * MiB:
                stop = True
        if stop or len(block) < window:
            break
        pos += window - 8
    if last_end is None:
        return None
    return Found(last_end, "pdf", "Documents (PDF)")


def _zip(fetch: Fetch) -> Found | None:
    head = fetch(0, 30)
    if head[:4] != b"PK\x03\x04":
        return None
    version, _flags, method = struct.unpack_from("<HHH", head, 4)
    name_len = struct.unpack_from("<H", head, 26)[0]
    if version > 100 or method not in (0, 8, 9, 12, 14, 93, 95, 98, 99) or name_len == 0 or name_len > 1024:
        return None
    first_name = fetch(30, name_len)
    window = 1 << 20
    pos = 0
    while pos < 4096 * MiB:
        block = fetch(pos, window + 22)
        if len(block) < 22:
            break
        index = 0
        while True:
            hit = block.find(b"PK\x05\x06", index)
            if hit < 0 or hit + 22 > len(block):
                break
            eocd = pos + hit
            # signature(4) disk(2) cd disk(2) entries here(2) entries total(2) cd size(4) cd offset(4) comment(2)
            _here, entries, cd_size, cd_offset, comment = struct.unpack_from("<8xHHIIH", block, hit)
            if cd_offset + cd_size == eocd and entries > 0:
                total = eocd + 22 + comment
                names = fetch(cd_offset, min(cd_size, 4 * MiB))
                return Found(total, *_zip_kind(first_name, names))
            index = hit + 1
        if len(block) < window + 22:
            break
        pos += window
    return None


def _zip_kind(first_name: bytes, central: bytes) -> tuple[str, str]:
    if first_name == b"mimetype" or b"mimetype" in central[:256]:
        mime = central
        if b"opendocument.text" in mime:
            return "odt", "Documents (ODT)"
        if b"opendocument.spreadsheet" in mime:
            return "ods", "Spreadsheets (ODS)"
        if b"epub" in mime:
            return "epub", "Books (EPUB)"
    if b"word/" in central:
        return "docx", "Documents (Word)"
    if b"xl/" in central:
        return "xlsx", "Spreadsheets (Excel)"
    if b"ppt/" in central:
        return "pptx", "Presentations (PowerPoint)"
    if b"AndroidManifest.xml" in central:
        return "apk", "Archives (ZIP)"
    if b"META-INF/MANIFEST.MF" in central:
        return "jar", "Archives (ZIP)"
    return "zip", "Archives (ZIP)"


def _ole(fetch: Fetch) -> Found | None:
    head = fetch(0, 512)
    if head[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or len(head) < 512:
        return None
    major, _bom, shift = struct.unpack_from("<HHH", head, 26)
    if (major, shift) not in ((3, 9), (4, 12)):
        return None
    sector = 1 << shift
    n_fat = struct.unpack_from("<I", head, 44)[0]
    dir_start = struct.unpack_from("<I", head, 48)[0]
    difat_start, n_difat = struct.unpack_from("<II", head, 68)
    if n_fat == 0 or n_fat > 100000:
        return None
    fat_sectors = list(struct.unpack_from("<109I", head, 76))
    fat_sectors = [s for s in fat_sectors if s < 0xFFFFFFFA][:n_fat]
    current = difat_start
    for _ in range(min(n_difat, 10000)):
        if current >= 0xFFFFFFFA:
            break
        block = fetch((current + 1) * sector, sector)
        values = struct.unpack(f"<{sector // 4}I", block.ljust(sector, b"\0"))
        fat_sectors += [s for s in values[:-1] if s < 0xFFFFFFFA]
        current = values[-1]
    fat_sectors = fat_sectors[:n_fat]
    highest = 0
    for index, fs in enumerate(fat_sectors):
        block = fetch((fs + 1) * sector, sector)
        if len(block) < sector:
            return None
        values = struct.unpack(f"<{sector // 4}I", block)
        for j in range(len(values) - 1, -1, -1):
            if values[j] != 0xFFFFFFFF:
                highest = max(highest, index * (sector // 4) + j)
                break
    total = (highest + 2) * sector
    root = fetch((dir_start + 1) * sector, 4 * sector) if dir_start < 0xFFFFFFFA else b""
    names = root.decode("utf-16-le", "ignore") if root else ""
    if "WordDocument" in names:
        return Found(total, "doc", "Documents (Word 97-2003)")
    if "Workbook" in names or "Book" in names:
        return Found(total, "xls", "Spreadsheets (Excel 97-2003)")
    if "PowerPoint" in names:
        return Found(total, "ppt", "Presentations (PowerPoint 97-2003)")
    if "__substg1.0_" in names or "__properties_version1.0" in names:
        return Found(total, "msg", "E-mail (Outlook MSG)")
    return Found(total, "ole", "Documents (other Office)")


def _sevenzip(fetch: Fetch) -> Found | None:
    head = fetch(0, 32)
    if head[:6] != b"7z\xbc\xaf\x27\x1c" or len(head) < 32:
        return None
    next_offset, next_size = struct.unpack_from("<QQ", head, 12)
    total = 32 + next_offset + next_size
    if next_size == 0 or total > 1 << 42:
        return None
    return Found(total, "7z", "Archives (7-Zip)")


def _rar(fetch: Fetch) -> Found | None:
    head = fetch(0, 8)
    if head[:7] == b"Rar!\x1a\x07\x00":
        pos = 7
        while pos < 64 * 1024 * MiB:
            hdr = fetch(pos, 11)
            if len(hdr) < 7:
                return None
            htype = hdr[2]
            flags, size = struct.unpack_from("<HH", hdr, 3)
            if size < 7 or htype < 0x72 or htype > 0x7B:
                return None
            add = 0
            if flags & 0x8000 and len(hdr) >= 11:
                add = struct.unpack_from("<I", hdr, 7)[0]
            pos += size + add
            if htype == 0x7B:
                return Found(pos, "rar", "Archives (RAR)")
        return None
    if head[:8] == b"Rar!\x1a\x07\x01\x00":
        pos = 8
        while pos < 64 * 1024 * MiB:
            hdr = fetch(pos, 32)
            if len(hdr) < 7:
                return None
            p = 4
            parsed = _rar_vint(hdr, p)
            if parsed is None:
                return None
            hsize, p = parsed
            start_body = p
            parsed = _rar_vint(hdr, p)
            if parsed is None:
                return None
            htype, p = parsed
            parsed = _rar_vint(hdr, p)
            if parsed is None:
                return None
            hflags, p = parsed
            data_size = 0
            if hflags & 0x01:
                parsed = _rar_vint(hdr, p)
                if parsed is None:
                    return None
                p = parsed[1]
            if hflags & 0x02:
                parsed = _rar_vint(hdr, p)
                if parsed is None:
                    return None
                data_size = parsed[0]
            if htype not in (1, 2, 3, 4, 5) or hsize == 0 or hsize > 2 * MiB:
                return None
            pos += start_body + hsize + data_size
            if htype == 5:
                return Found(pos, "rar", "Archives (RAR)")
        return None
    return None


def _rar_vint(data: bytes, pos: int) -> tuple[int, int] | None:
    value = 0
    shift = 0
    while pos < len(data) and shift < 64:
        byte = data[pos]
        value |= (byte & 0x7F) << shift
        pos += 1
        if not byte & 0x80:
            return value, pos
        shift += 7
    return None


def _sqlite(fetch: Fetch) -> Found | None:
    head = fetch(0, 100)
    if head[:16] != b"SQLite format 3\x00" or len(head) < 100:
        return None
    page = struct.unpack(">H", head[16:18])[0]
    page = 65536 if page == 1 else page
    if page < 512 or page & (page - 1):
        return None
    change, count = struct.unpack_from(">II", head, 24)
    valid_for = struct.unpack_from(">I", head, 92)[0]
    if count == 0 or valid_for != change:
        return None
    return Found(page * count, "sqlite", "Databases (SQLite)")


def _pst(fetch: Fetch) -> Found | None:
    head = fetch(0, 0xC0)
    if head[:4] != b"!BDN" or len(head) < 0xC0:
        return None
    version = struct.unpack_from("<H", head, 10)[0]
    if version >= 23:
        size = struct.unpack_from("<Q", head, 0xB8)[0]
    elif version in (14, 15):
        size = struct.unpack_from("<I", head, 0xA8)[0]
    else:
        return None
    if size < 0x4400:
        return None
    return Found(size, "pst", "E-mail (Outlook PST)")


def _tiff(fetch: Fetch) -> Found | None:
    head = fetch(0, 16)
    if head[:4] == b"II*\x00":
        endian = "<"
    elif head[:4] == b"MM\x00*":
        endian = ">"
    else:
        return None
    first_ifd = struct.unpack(endian + "I", head[4:8])[0]
    if first_ifd < 8 or first_ifd > 64 * MiB:
        return None
    highest = first_ifd
    tags_seen: set[int] = set()
    queue = [first_ifd]
    visited: set[int] = set()
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
    pairs = {273: 279, 324: 325, 513: 514}  # strips, tiles, JPEG thumbnail
    while queue and len(visited) < 64:
        ifd = queue.pop()
        if ifd in visited or ifd > 512 * MiB:
            continue
        visited.add(ifd)
        count_raw = fetch(ifd, 2)
        if len(count_raw) < 2:
            return None
        count = struct.unpack(endian + "H", count_raw)[0]
        if count == 0 or count > 1000:
            if ifd == first_ifd:
                return None
            continue
        table = fetch(ifd + 2, count * 12 + 4)
        highest = max(highest, ifd + 2 + count * 12 + 4)
        offsets: dict[int, list[int]] = {}
        for i in range(count):
            tag, ftype, n = struct.unpack_from(endian + "HHI", table, i * 12)
            tags_seen.add(tag)
            unit = sizes.get(ftype)
            if unit is None:
                continue
            nbytes = unit * n
            if nbytes > 4:
                ptr = struct.unpack_from(endian + "I", table, i * 12 + 8)[0]
                highest = max(highest, ptr + nbytes)
                values_raw = fetch(ptr, min(nbytes, 1 << 16))
            else:
                values_raw = table[i * 12 + 8:i * 12 + 8 + nbytes]
            if ftype in (3, 4) and tag in (273, 279, 324, 325, 513, 514, 330, 34665):
                fmt = "H" if ftype == 3 else "I"
                k = min(n, len(values_raw) // struct.calcsize(fmt))
                values = list(struct.unpack(endian + fmt * k, values_raw[:k * struct.calcsize(fmt)]))
                offsets[tag] = values
                if tag in (330, 34665):
                    queue.extend(values)
        for off_tag, len_tag in pairs.items():
            if off_tag in offsets and len_tag in offsets:
                for o, ln in zip(offsets[off_tag], offsets[len_tag], strict=False):
                    highest = max(highest, o + ln)
        nxt_raw = table[count * 12:count * 12 + 4]
        if len(nxt_raw) == 4:
            nxt = struct.unpack(endian + "I", nxt_raw)[0]
            if nxt:
                queue.append(nxt)
    if 256 not in tags_seen and 273 not in tags_seen:
        return None
    if highest > 1024 * MiB:
        return None
    if head[8:10] == b"CR":
        return Found(highest, "cr2", "Camera RAW (CR2)", exact=False)
    make = fetch(0, 4096)
    if b"NIKON" in make:
        return Found(highest, "nef", "Camera RAW (NEF)", exact=False)
    if b"SONY" in make:
        return Found(highest, "arw", "Camera RAW (ARW)", exact=False)
    if b"OLYMPUS" in make:
        return Found(highest, "orf", "Camera RAW (ORF)", exact=False)
    if b"PENTAX" in make:
        return Found(highest, "pef", "Camera RAW (PEF)", exact=False)
    if 50706 in tags_seen:
        return Found(highest, "dng", "Camera RAW (DNG)", exact=False)
    return Found(highest, "tif", "Pictures (TIFF)", exact=False)


def _psd(fetch: Fetch) -> Found | None:
    head = fetch(0, 26)
    if head[:4] != b"8BPS" or len(head) < 26:
        return None
    version = struct.unpack(">H", head[4:6])[0]
    if version not in (1, 2):
        return None
    channels, height, width, depth, mode = struct.unpack(">HIIHH", head[12:26])
    if not 1 <= channels <= 56 or depth not in (1, 8, 16, 32) or mode > 15:
        return None
    pos = 26
    for wide in (False, False, version == 2):
        raw = fetch(pos, 8 if wide else 4)
        if len(raw) < (8 if wide else 4):
            return None
        length = struct.unpack(">Q" if wide else ">I", raw)[0]
        pos += (8 if wide else 4) + length
    comp_raw = fetch(pos, 2)
    if len(comp_raw) < 2:
        return None
    compression = struct.unpack(">H", comp_raw)[0]
    pos += 2
    row_bytes = (width * depth + 7) // 8
    if compression == 0:
        pos += row_bytes * height * channels
    elif compression == 1:
        rows = height * channels
        unit = 4 if version == 2 else 2
        counts = fetch(pos, rows * unit)
        if len(counts) < rows * unit:
            return None
        fmt = ">" + ("I" if version == 2 else "H") * rows
        pos += rows * unit + sum(struct.unpack(fmt, counts))
    else:
        return None
    return Found(pos, "psd", "Pictures (Photoshop)")


@dataclass(frozen=True)
class Signature:
    magic: bytes
    offset: int            # where the magic sits in the first sector
    measure: Callable[[Fetch], Found | None]
    group: str             # for the user's type filter


SIGNATURES: list[Signature] = [
    Signature(b"\xff\xd8\xff", 0, _jpeg, "Photos"),
    Signature(b"\x89PNG", 0, _png, "Pictures"),
    Signature(b"GIF8", 0, _gif, "Pictures"),
    Signature(b"BM", 0, _bmp, "Pictures"),
    Signature(b"II*\x00", 0, _tiff, "Photos"),
    Signature(b"MM\x00*", 0, _tiff, "Photos"),
    Signature(b"8BPS", 0, _psd, "Pictures"),
    Signature(b"ftyp", 4, _iso_bmff, "Videos"),
    Signature(b"RIFF", 0, _riff, "Videos"),
    Signature(b"\x1a\x45\xdf\xa3", 0, _ebml, "Videos"),
    Signature(b"ID3", 0, _mp3, "Audio"),
    Signature(b"OggS", 0, _ogg, "Audio"),
    Signature(b"%PDF-", 0, _pdf, "Documents"),
    Signature(b"PK\x03\x04", 0, _zip, "Documents"),
    Signature(b"\xd0\xcf\x11\xe0", 0, _ole, "Documents"),
    Signature(b"7z\xbc\xaf", 0, _sevenzip, "Archives"),
    Signature(b"Rar!", 0, _rar, "Archives"),
    Signature(b"SQLite f", 0, _sqlite, "Databases"),
    Signature(b"!BDN", 0, _pst, "E-mail"),
]

GROUPS = sorted({sig.group for sig in SIGNATURES})


class CarvedVolume(Volume):
    kind = "Carved"

    def __init__(self, reader: RescueReader) -> None:
        super().__init__(reader, 0, reader.size, "Files found by signature")
        self.cluster_size = SECTOR

    def layout(self, node: Node) -> FileLayout:
        offset, size = node.ref
        return FileLayout(size, [Extent(0, size, offset)])


class Carver:
    """Feed it consecutive chunks of the disk; it collects carved files."""

    def __init__(self, reader: RescueReader, groups: set[str] | None = None,
                 limit_per_type: int = 200_000) -> None:
        self.reader = reader
        self.volume = CarvedVolume(reader)
        self.root = Node("Files found by signature", F.DIR | F.VIRTUAL | F.VOLUME, volume=self.volume)
        self.volume.root = self.root
        sigs = [s for s in SIGNATURES if groups is None or s.group in groups]
        self._by_offset: dict[int, list[Signature]] = {}
        for sig in sigs:
            self._by_offset.setdefault(sig.offset, []).append(sig)
        self._tables: dict[int, bytes] = {}
        for offset, group in self._by_offset.items():
            table = bytearray(256)
            for sig in group:
                table[sig.magic[0]] = 1
            self._tables[offset] = bytes(table)
        self.skip_until = 0
        self.count = 0
        self.limit_per_type = limit_per_type
        self._per_type: dict[str, int] = {}
        self._groups: dict[str, Node] = {}

    def _fetcher(self, start: int) -> Fetch:
        """Byte access relative to ``start``, served from a 1 MiB read window."""
        size = self.reader.size
        reader = self.reader
        window = 1 << 20
        cache: dict[str, object] = {"base": -1, "data": b""}

        def fetch(offset: int, length: int) -> bytes:
            absolute = start + offset
            if absolute >= size or length <= 0 or offset < 0:
                return b""
            length = min(length, size - absolute)
            base = cache["base"]
            data = cache["data"]
            assert isinstance(base, int) and isinstance(data, bytes)
            if base <= absolute and absolute + length <= base + len(data):
                return data[absolute - base:absolute - base + length]
            if length > window:
                return bytes(reader.read(absolute, length, ReadMode.FAST).data)
            block_base = absolute - absolute % SECTOR
            block_len = min(max(window, absolute + length - block_base), size - block_base)
            data = bytes(reader.read(block_base, block_len, ReadMode.FAST).data)
            cache["base"], cache["data"] = block_base, data
            return data[absolute - block_base:absolute - block_base + length]

        return fetch

    def feed(self, base: int, data: bytes | bytearray) -> None:
        """Examine every sector start inside ``data`` (which begins at ``base``)."""
        view = bytes(data)
        candidates: set[int] = set()
        for offset, table in self._tables.items():
            firsts = view[offset::SECTOR]
            marks = firsts.translate(table)
            index = marks.find(1)
            while index >= 0:
                candidates.add(index)
                index = marks.find(1, index + 1)
        for index in sorted(candidates):
            pos = index * SECTOR
            absolute = base + pos
            if absolute < self.skip_until:
                continue
            for offset, sigs in self._by_offset.items():
                for sig in sigs:
                    magic_at = pos + offset
                    if view[magic_at:magic_at + len(sig.magic)] != sig.magic:
                        continue
                    try:
                        found = sig.measure(self._fetcher(absolute))
                    except (struct.error, IndexError, ValueError, OverflowError):
                        found = None
                    if found is None or found.size <= 0:
                        continue
                    if absolute + found.size > self.reader.size:
                        continue
                    self._add(absolute, found)
                    self.skip_until = absolute + found.size
                    break
                else:
                    continue
                break

    def _add(self, offset: int, found: Found) -> None:
        if self._per_type.get(found.kind, 0) >= self.limit_per_type:
            return
        self._per_type[found.kind] = self._per_type.get(found.kind, 0) + 1
        group = self._groups.get(found.kind)
        if group is None:
            group = Node(found.kind, F.DIR | F.VIRTUAL, volume=self.volume)
            self._groups[found.kind] = group
            self.root.add(group)
        sector = offset // self.reader.sector_size
        name = f"{found.ext.upper()}_{sector:011d}.{found.ext}"
        flags = F.CARVED | F.DELETED
        group.add(Node(name, flags, found.size, volume=self.volume, ref=(offset, found.size)))
        self.count += 1
