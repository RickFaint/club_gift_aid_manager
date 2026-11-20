"""Byte-level validation for declaration evidence uploads."""

from __future__ import annotations

import re
import struct
import zlib


ALLOWED_EVIDENCE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "application/pdf"})


def detect_evidence_mime(data: bytes) -> str | None:
    """Return the MIME type only when *data* has a valid supported structure."""
    if _valid_png(data):
        return "image/png"
    if _valid_jpeg(data):
        return "image/jpeg"
    if _valid_webp(data):
        return "image/webp"
    if _valid_pdf(data):
        return "application/pdf"
    return None


def _valid_png(data: bytes) -> bool:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    names: list[bytes] = []
    compressed = bytearray()
    valid_header = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        end = offset + 12 + length
        if end > len(data):
            return False
        name = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : end])[0]
        if zlib.crc32(name + payload) & 0xFFFFFFFF != expected_crc:
            return False
        names.append(name)
        if name == b"IHDR":
            if names != [b"IHDR"] or length != 13:
                return False
            width, height, bit_depth, colour_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", payload)
            )
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            valid_header = (
                width > 0
                and height > 0
                and bit_depth in valid_depths.get(colour_type, set())
                and compression == filtering == 0
                and interlace in {0, 1}
            )
        if name == b"IDAT":
            compressed.extend(payload)
        if name == b"IEND" and length != 0:
            return False
        offset = end
        if name == b"IEND":
            break
    if offset != len(data) or not names or names[0] != b"IHDR":
        return False
    if not valid_header or names[-1] != b"IEND" or b"IDAT" not in names:
        return False
    try:
        return len(zlib.decompress(compressed)) > 0
    except zlib.error:
        return False


def _valid_jpeg(data: bytes) -> bool:
    if len(data) < 4 or data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
        return False
    offset = 2
    saw_frame = False
    saw_scan = False
    while offset < len(data):
        if data[offset] != 0xFF:
            return False
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            return False
        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            return saw_frame and saw_scan and offset == len(data)
        if marker in {0x01, *range(0xD0, 0xD8)}:
            continue
        if offset + 2 > len(data):
            return False
        length = struct.unpack(">H", data[offset : offset + 2])[0]
        if length < 2 or offset + length > len(data):
            return False
        if marker in range(0xC0, 0xD0) and marker not in {0xC4, 0xC8, 0xCC}:
            saw_frame = True
        offset += length
        if marker == 0xDA:
            saw_scan = True
            while offset < len(data) - 1:
                if data[offset] != 0xFF:
                    offset += 1
                    continue
                following = data[offset + 1]
                if following == 0x00 or 0xD0 <= following <= 0xD7:
                    offset += 2
                    continue
                break
    return False


def _valid_webp(data: bytes) -> bool:
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return False
    if struct.unpack("<I", data[4:8])[0] + 8 != len(data):
        return False
    offset = 12
    first = True
    while offset + 8 <= len(data):
        name = data[offset : offset + 4]
        length = struct.unpack("<I", data[offset + 4 : offset + 8])[0]
        end = offset + 8 + length
        if end > len(data):
            return False
        if first and name not in {b"VP8 ", b"VP8L", b"VP8X"}:
            return False
        if first:
            payload = data[offset + 8 : end]
            if name == b"VP8 " and (length < 10 or payload[3:6] != b"\x9d\x01\x2a"):
                return False
            if name == b"VP8L" and (length < 5 or payload[0] != 0x2F or payload[4] & 0xE0):
                return False
            if name == b"VP8X" and length != 10:
                return False
        first = False
        offset = end + (length & 1)
    return not first and offset == len(data)


def _valid_pdf(data: bytes) -> bool:
    header = re.search(rb"%PDF-1\.[0-7]|%PDF-2\.0", data[:1024])
    if header is None or re.search(rb"/Type\s*/Catalog\b", data) is None:
        return False
    starts = list(re.finditer(rb"startxref\s+(\d+)\s+%%EOF\s*\Z", data))
    if not starts:
        return False
    xref_offset = int(starts[-1].group(1))
    return 0 <= xref_offset < len(data) and (
        data[xref_offset : xref_offset + 4] == b"xref"
        or re.match(rb"\d+\s+\d+\s+obj\b", data[xref_offset:]) is not None
    )
