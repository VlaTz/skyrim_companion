"""Чтение файлов игры: архивы BSA и таблицы строк (*_russian.strings/.dlstrings/.ilstrings).

Общее для сервиса (тексты журнала квестов) и инструментов в tools/ (голоса, реплики).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

FLAG_COMPRESSED = 0x4
FLAG_EMBED_NAMES = 0x100
SIZE_COMPRESS_TOGGLE = 0x40000000
LZ4_FRAME_MAGIC = b"\x04\x22\x4d\x18"


@dataclass
class Entry:
    folder: str
    name: str
    size: int
    offset: int
    compressed: bool


def read_bsa_index(f) -> tuple[list[Entry], int]:
    magic, ver, folder_off, flags, nfolders, nfiles, _, names_len, _ = struct.unpack(
        "<4sIIIIIIII", f.read(36))
    if magic != b"BSA\0" or ver not in (104, 105):
        raise ValueError(f"Не BSA Скайрима (magic={magic!r}, version={ver})")

    f.seek(folder_off)
    rec_size = 24 if ver == 105 else 16
    counts = [struct.unpack_from("<I", f.read(rec_size), 8)[0] for _ in range(nfolders)]

    raw: list[tuple[str, int, int]] = []
    for count in counts:
        n = f.read(1)[0]
        folder = f.read(n).rstrip(b"\0").decode("cp1252")
        for _ in range(count):
            _, size, offset = struct.unpack("<QII", f.read(16))
            raw.append((folder, size, offset))

    names = f.read(names_len).split(b"\0")
    default_compressed = bool(flags & FLAG_COMPRESSED)
    entries = [
        Entry(folder, names[i].decode("cp1252"), size & ~SIZE_COMPRESS_TOGGLE, offset,
              default_compressed != bool(size & SIZE_COMPRESS_TOGGLE))
        for i, (folder, size, offset) in enumerate(raw)
    ]
    return entries, flags


def read_file(f, e: Entry, flags: int) -> bytes:
    f.seek(e.offset)
    size = e.size
    if flags & FLAG_EMBED_NAMES:
        n = f.read(1)[0]
        f.seek(n, 1)
        size -= n + 1
    data = f.read(size)
    if e.compressed:  # первые 4 байта — исходный размер; дальше zlib (LE) или кадр LZ4 (SE, версия 105)
        if data[4:8] == LZ4_FRAME_MAGIC:
            import lz4.frame
            return lz4.frame.decompress(data[4:])
        data = zlib.decompress(data[4:])
    return data


def parse_strings(data: bytes, length_prefixed: bool) -> dict[int, str]:
    count, _ = struct.unpack_from("<II", data, 0)
    base = 8 + count * 8
    out = {}
    for i in range(count):
        sid, off = struct.unpack_from("<II", data, 8 + i * 8)
        pos = base + off
        if length_prefixed:
            pos += 4
        end = data.index(b"\0", pos)
        raw = data[pos:end]
        try:
            out[sid] = raw.decode("utf-8")
        except UnicodeDecodeError:
            out[sid] = raw.decode("cp1251")
    return out


def load_strings(bsa: Path, kind: str, language: str = "russian") -> dict[str, dict[int, str]]:
    """Таблицы строк из архива: {"skyrim": {id: текст}, "update": ..., ...}.
    kind: "strings" (имена), "dlstrings" (журнал, книги), "ilstrings" (реплики)."""
    tables = {}
    suffix = f"_{language}.{kind}"
    with open(bsa, "rb") as f:
        entries, flags = read_bsa_index(f)
        for e in entries:
            if e.folder == "strings" and e.name.endswith(suffix):
                tables[e.name[:-len(suffix)]] = parse_strings(read_file(f, e, flags), kind != "strings")
    return tables
