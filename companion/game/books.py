"""Книги игры: название и текст по файлу плагина и номеру записи (BOOK в Skyrim.esm и DLC).

Плагин шлёт, какую книгу открыл Владос (`book_read`) или отдал Гослингу (`gosling_items`), — файлом и номером
записи; текст берём отсюда: название — `*_russian.strings`, текст — `*_russian.dlstrings` (Skyrim -
Interface.bsa). 1045 книг с текстом, ~2,5 млн знаков; грузится при первом обращении (~1 с).
Разметку книги (<font>, <p>, [pagebreak]) убираем — модели нужен только текст.
"""

from __future__ import annotations

import logging
import re
import struct
import threading
import zlib
from dataclasses import dataclass
from pathlib import Path

from companion.game.dialogue import FLAG_COMPRESSED, PLUGINS, iter_subrecords
from companion.game.gamefiles import load_strings

log = logging.getLogger(__name__)

TAG = re.compile(r"<[^>]*>|\[pagebreak\]", re.I)


def plugin_records(path: Path, types: set[bytes]):
    """Записи нужных типов из верхних групп плагина: (тип, форма, {подзапись: [данные]})."""
    data = path.read_bytes()
    pos = 24 + struct.unpack_from("<I", data, 4)[0]
    while pos < len(data):
        size = struct.unpack_from("<I", data, pos + 4)[0]
        if data[pos + 8:pos + 12] in types and struct.unpack_from("<i", data, pos + 12)[0] == 0:
            p = pos + 24
            while p < pos + size:
                typ, rsize = data[p:p + 4], struct.unpack_from("<I", data, p + 4)[0]
                if typ != b"GRUP":
                    flags, form = struct.unpack_from("<II", data, p + 8)
                    body = data[p + 24:p + 24 + rsize]
                    if flags & FLAG_COMPRESSED:
                        body = zlib.decompress(body[4:])
                    subs: dict[bytes, list[bytes]] = {}
                    for sub, payload in iter_subrecords(body):
                        subs.setdefault(sub, []).append(payload)
                    yield typ, form, subs
                p += rsize if typ == b"GRUP" else 24 + rsize
        pos += size


def clean(text: str) -> str:
    """Текст книги без разметки, абзацами."""
    text = TAG.sub("", text).replace("\r", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def book_key(plugin_file: str, form: int) -> str:
    """«skyrim:0a3f12» — ключ книги в памяти (по нему же помним, что прочитано)."""
    return f"{Path(plugin_file).stem.lower()}:{form & 0xFFFFFF:06x}"


@dataclass(frozen=True)
class Book:
    key: str
    title: str
    text: str


class BookLibrary:
    """Все книги игры по ключу. Потокобезопасно."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._lock = threading.Lock()
        self._books: dict[str, Book] | None = None

    def get(self, plugin_file: str, form: int) -> Book | None:
        return self.by_key(book_key(plugin_file, form))

    def by_key(self, key: str) -> Book | None:
        return self._load().get(key.lower())

    def _load(self) -> dict[str, Book]:
        with self._lock:
            if self._books is None:
                self._books = {}
                try:
                    names = load_strings(self.data_dir / "Skyrim - Interface.bsa", "strings")
                    texts = load_strings(self.data_dir / "Skyrim - Interface.bsa", "dlstrings")
                    for plugin in PLUGINS:
                        path = self.data_dir / f"{plugin}.esm"
                        if not path.is_file():
                            continue
                        for _typ, form, subs in plugin_records(path, {b"BOOK"}):
                            if b"DESC" not in subs:
                                continue
                            sid = lambda key: struct.unpack_from("<I", subs[key][0])[0] if key in subs else None
                            title = (names.get(plugin, {}).get(sid(b"FULL")) or names.get("skyrim", {}).get(sid(b"FULL"))
                                     or "")
                            text = texts.get(plugin, {}).get(sid(b"DESC")) or texts.get("skyrim", {}).get(sid(b"DESC"))
                            if title and text:
                                # Запись создана в плагине-владельце (индекс мода в номере): DLC может править
                                # книги Skyrim.esm — ключ по номеру без индекса, как шлёт плагин (GetLocalFormID).
                                key = book_key(plugin, form)
                                self._books[key] = Book(key, title.strip(), clean(text))
                    log.info("Книги игры: %d", len(self._books))
                except OSError as e:
                    log.warning("Не прочитать книги игры (%s)", e)
            return self._books
