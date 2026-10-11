"""Unlocking encrypted APFS volumes with their password: external drives formatted "APFS
(Encrypted)", FileVault on a removed Mac drive, encrypted Time Machine backups.

How APFS keeps the keys (Apple File System Reference, "Encryption"):

* The container keybag (where ``nx_keylocker`` points) is encrypted with AES-XTS, using the
  container's UUID as both keys. For every encrypted volume it holds the volume's key (VEK),
  wrapped, and where that volume's own keybag is.
* The volume keybag is encrypted the same way with the volume's UUID. It holds one wrapped
  key-encrypting key (KEK) per way in (the user's password, a recovery key), and the password hint.
* A password unwraps a KEK: PBKDF2-HMAC-SHA256 with the record's salt and rounds gives the AES key
  for an RFC 3394 key unwrap, whose built-in check fails on a wrong password. The KEK unwraps the
  VEK.
* The VEK is an AES-XTS key (two 128-bit halves). It decrypts the volume's file-system tree nodes
  and the files' data in 512-byte units; the tweak of a unit is its block's number (for file data:
  the extent's crypto id, the block it was first written to) times the units per block, plus the
  unit's place in the block.

Nothing here writes anything: the keys live in memory for the session only.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

from ..rescue.reader import RescueReader

SECTOR = 512
TAG_VOLUME_KEY = 2
TAG_UNLOCK_RECORDS = 3
TAG_PASSWORD_HINT = 4
TYPE_CONTAINER_KEYBAG = 0x6B657973      # "keys"
TYPE_VOLUME_KEYBAG = 0x72656373         # "recs"


class CryptoUnavailable(RuntimeError):
    """The 'cryptography' package (AES) is missing."""


def _primitives():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap
    except ImportError as exc:                     # pragma: no cover - packaged builds include it
        raise CryptoUnavailable("Unlocking encrypted APFS volumes needs the 'cryptography' package.") from exc
    return Cipher, algorithms, modes, aes_key_unwrap, InvalidUnwrap


def xts_decrypt(key: bytes, data: bytes, sector: int) -> bytes:
    """Decrypt ``data`` (whole 512-byte units); the first unit's tweak is ``sector``."""
    cipher_class, algorithms, modes, _unwrap, _invalid = _primitives()
    algorithm = algorithms.AES(key)
    out = bytearray(len(data))
    for pos in range(0, len(data) - SECTOR + 1, SECTOR):
        decryptor = cipher_class(algorithm, modes.XTS((sector + pos // SECTOR).to_bytes(16, "little"))).decryptor()
        out[pos:pos + SECTOR] = decryptor.update(data[pos:pos + SECTOR]) + decryptor.finalize()
    return bytes(out)


def self_test() -> None:
    """Check the AES pieces against published test vectors (raises if one is wrong or missing):
    the packaged app runs this to prove it can unlock volumes."""
    cipher_class, algorithms, modes, unwrap, _invalid = _primitives()
    # RFC 3394, 4.1: a 128-bit key wrapped with a 128-bit key.
    kek = bytes.fromhex("000102030405060708090A0B0C0D0E0F")
    wrapped = bytes.fromhex("1FA68B0A8112B447AEF34BD8FB5A7B829D3E862371D2CFE5")
    if unwrap(kek, wrapped) != bytes.fromhex("00112233445566778899AABBCCDDEEFF"):
        raise RuntimeError("AES key unwrap gives the wrong answer")
    # IEEE 1619-2007, XTS-AES-128 vector 1: zero keys, data unit 0, 32 zero bytes.
    decryptor = cipher_class(algorithms.AES(bytes(32)), modes.XTS(bytes(16))).decryptor()
    secret = bytes.fromhex("917cf69ebd68b2ec9b9fe9a3eadda692cd43d2f59598ed858c02c2652fbf922e")
    if decryptor.update(secret) + decryptor.finalize() != bytes(32):
        raise RuntimeError("AES-XTS gives the wrong answer")
    # Lifeboat's own unit-by-unit decryption: two units, the second with the next tweak.
    key = hashlib.sha256(b"lifeboat").digest()
    plain = bytes(range(256)) * 4
    encrypted = b""
    for unit in range(2):
        encryptor = cipher_class(algorithms.AES(key), modes.XTS((7 + unit).to_bytes(16, "little"))).encryptor()
        encrypted += encryptor.update(plain[unit * SECTOR:(unit + 1) * SECTOR]) + encryptor.finalize()
    if xts_decrypt(key, encrypted, 7) != plain:
        raise RuntimeError("AES-XTS unit tweaks are wrong")


@dataclass
class KeybagEntry:
    uuid: bytes
    tag: int
    data: bytes


def parse_keybag(block: bytes, kind: int) -> list[KeybagEntry]:
    """The entries of a decrypted keybag object, or [] if it isn't one (wrong key, damage)."""
    from .apfs import checksum_ok

    if len(block) < 48 or not checksum_ok(block) or struct.unpack_from("<I", block, 24)[0] != kind:
        return []
    _version, count, _nbytes = struct.unpack_from("<HHI", block, 32)
    entries = []
    pos = 48
    for _ in range(count):
        if pos + 24 > len(block):
            break
        uuid = bytes(block[pos:pos + 16])
        tag, length = struct.unpack_from("<HH", block, pos + 16)
        data = bytes(block[pos + 24:pos + 24 + length])
        entries.append(KeybagEntry(uuid, tag, data))
        pos += (24 + length + 15) & ~15
    return entries


def _der(data: bytes) -> dict[int, bytes]:
    """The tagged values inside one DER-encoded sequence (enough for APFS's key blobs)."""
    out: dict[int, bytes] = {}
    pos = 0
    while pos + 2 <= len(data):
        tag = data[pos]
        length = data[pos + 1]
        pos += 2
        if length & 0x80:
            size = length & 0x7F
            length = int.from_bytes(data[pos:pos + size], "big")
            pos += size
        out[tag] = data[pos:pos + length]
        pos += length
    return out


def _key_blob(blob: bytes) -> dict[int, bytes]:
    """The inner key fields of a wrapped-key blob: SEQUENCE { ..., [3] SEQUENCE { fields } }."""
    outer = _der(blob).get(0x30, b"")
    return _der(_der(outer).get(0xA3, b""))


class ApfsKeys:
    """Reads the container's keybags and turns a password into a volume key."""

    def __init__(self, reader: RescueReader, offset: int, block_size: int, container_uuid: bytes,
                 keylocker: tuple[int, int]) -> None:
        self.reader = reader
        self.offset = offset
        self.block_size = block_size
        self.container_uuid = container_uuid
        self.keylocker = keylocker
        self._container: list[KeybagEntry] | None = None

    def _bag(self, paddr: int, count: int, uuid: bytes, kind: int) -> list[KeybagEntry]:
        if paddr <= 0 or not 0 < count <= 16:
            return []
        start = self.offset + paddr * self.block_size
        length = count * self.block_size
        if start + length > self.reader.size:
            return []
        raw = bytes(self.reader.read_critical(start, length).data)
        plain = xts_decrypt(uuid + uuid, raw, paddr * (self.block_size // SECTOR))
        entries: list[KeybagEntry] = []
        for i in range(count):
            entries.extend(parse_keybag(plain[i * self.block_size:(i + 1) * self.block_size], kind))
        return entries

    def container_entries(self) -> list[KeybagEntry]:
        if self._container is None:
            paddr, count = self.keylocker
            self._container = self._bag(paddr, count, self.container_uuid, TYPE_CONTAINER_KEYBAG)
        return self._container

    def volume_entries(self, volume_uuid: bytes) -> list[KeybagEntry]:
        where = next((e.data for e in self.container_entries()
                      if e.uuid == volume_uuid and e.tag == TAG_UNLOCK_RECORDS and len(e.data) >= 16), None)
        if where is None:
            return []
        paddr, count = struct.unpack_from("<QQ", where, 0)
        return self._bag(paddr, count, volume_uuid, TYPE_VOLUME_KEYBAG)

    def hint(self, volume_uuid: bytes) -> str:
        """The password hint saved with the volume, or ""."""
        for entry in self.volume_entries(volume_uuid):
            if entry.tag == TAG_PASSWORD_HINT:
                return entry.data.split(b"\0", 1)[0].decode("utf-8", "replace").strip()
        return ""

    def has_keys(self, volume_uuid: bytes) -> bool:
        return any(e.uuid == volume_uuid and e.tag == TAG_VOLUME_KEY for e in self.container_entries())

    def unlock(self, volume_uuid: bytes, password: str) -> bytes | None:
        """The volume's AES-XTS key, or None if ``password`` doesn't open it."""
        _cipher, _algorithms, _modes, unwrap, invalid = _primitives()
        wrapped_vek = next((e.data for e in self.container_entries()
                            if e.uuid == volume_uuid and e.tag == TAG_VOLUME_KEY), None)
        if wrapped_vek is None:
            return None
        vek_fields = _key_blob(wrapped_vek)
        for record in self.volume_entries(volume_uuid):
            if record.tag != TAG_UNLOCK_RECORDS:
                continue
            fields = _key_blob(record.data)
            wrapped_kek, rounds, salt = fields.get(0x83), fields.get(0x84), fields.get(0x85)
            if not wrapped_kek or not rounds or not salt:
                continue
            iterations = int.from_bytes(rounds, "big")
            if not 0 < iterations <= 10_000_000:
                continue
            derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, 32)
            try:
                kek = unwrap(derived[:16] if len(wrapped_kek) == 24 else derived, wrapped_kek)
            except invalid:
                continue                                  # not this record's password
            wrapped = vek_fields.get(0x83, b"")
            try:
                vek = unwrap(kek, wrapped)
            except invalid:
                continue
            if len(vek) == 16:                            # older 128-bit key: widen it like macOS does
                vek += hashlib.sha256(vek + vek_fields.get(0x81, b"")).digest()[:16]
            return vek
        return None
