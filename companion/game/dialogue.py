"""Тексты реплик игры по записи INFO: что сказал NPC рядом (событие npc_line от плагина).

Реплика в плагине (Skyrim.esm, Dawnguard.esm ...) — запись INFO с ответами по порядку: TRDT (эмоция,
номер ответа) и NAM1 (id строки в *_russian.ilstrings из Skyrim - Interface.bsa). Плагин шлёт файл
и номер записи без индекса мода — по ним и ищем. Всё грузится один раз, за ~0,4 с.
Тем же пользуется tools/extract_dialogue.py (тексты и эмоции для датасета голоса).

Что сказал игрок (выбор в меню разговора) — тоже из файлов: ответ NPC (INFO) лежит в теме (DIAL, группа 7
с номером темы); строка игрока — подсказка RNAM у INFO, а нет её — название темы FULL (*_russian.strings).
Только у тем «реплика игрока» (тип 0) и «команда спутнику» (тип 1): у приветствий и сцен выбора игрока нет.
"""

from __future__ import annotations

import logging
import re
import struct
import threading
import zlib
from pathlib import Path

from companion.game.gamefiles import load_strings

log = logging.getLogger(__name__)

PLUGINS = ["skyrim", "update", "dawnguard", "hearthfires", "dragonborn"]
EMOTIONS = ["neutral", "anger", "disgust", "fear", "sad", "happy", "surprise", "puzzled"]
PLAYER_TOPICS = (0, 1)  # тип темы: реплика игрока, команда спутнику
EDITOR_ID = re.compile(r"[A-Za-z0-9_]+")  # «DB02Captive2Intimidate» — служебное имя темы, не текст
TAG = re.compile(r"<[^>]+>")
FLAG_COMPRESSED = 0x00040000


def iter_records(data: bytes, start: int, end: int):
    """Обходит записи, раскрывая только группы диалогов (DIAL и их INFO)."""
    pos = start
    while pos < end:
        typ = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        if typ == b"GRUP":
            label = data[pos + 8:pos + 12]
            gtype = struct.unpack_from("<i", data, pos + 12)[0]
            if (gtype == 0 and label == b"DIAL") or gtype == 7:
                yield from iter_records(data, pos + 24, pos + size)
            pos += size
        else:
            yield typ, struct.unpack_from("<I", data, pos + 8)[0], \
                struct.unpack_from("<I", data, pos + 12)[0], data[pos + 24:pos + 24 + size]
            pos += 24 + size


def iter_subrecords(body: bytes):
    pos, big = 0, None
    while pos < len(body):
        typ = body[pos:pos + 4]
        size = struct.unpack_from("<H", body, pos + 4)[0]
        pos += 6
        if big is not None:
            size, big = big, None
        if typ == b"XXXX":
            big = struct.unpack_from("<I", body, pos)[0]
            pos += size
            continue
        yield typ, body[pos:pos + size]
        pos += size


def load_responses(plugin: Path) -> dict[tuple[int, int], tuple[str, int, int]]:
    """(formID & 0xFFFFFF, номер ответа) -> (эмоция, сила, id строки)."""
    data = plugin.read_bytes()
    out = {}
    header_size = 24 + struct.unpack_from("<I", data, 4)[0]
    for typ, flags, form, body in iter_records(data, header_size, len(data)):
        if typ != b"INFO":
            continue
        if flags & FLAG_COMPRESSED:
            body = zlib.decompress(body[4:])
        key = None
        for sub, payload in iter_subrecords(body):
            if sub == b"TRDT":
                emo, value = struct.unpack_from("<Ii", payload, 0)
                key = (form & 0xFFFFFF, payload[12])
                # у части ответов NAM1 нет (пустой текст) — сохраняем эмоцию и без него
                out[key] = (EMOTIONS[emo] if emo < len(EMOTIONS) else str(emo), value, 0)
            elif sub == b"NAM1" and key:
                emo, value, _ = out[key]
                out[key] = (emo, value, struct.unpack_from("<I", payload)[0])
                key = None
    return out


def load_prompts(plugin: Path) -> dict[int, tuple[int, int | None, int | None]]:
    """INFO (formID & 0xFFFFFF) -> (тип темы, id строки FULL темы, id строки RNAM ответа)."""
    data = plugin.read_bytes()
    dials: dict[int, tuple[int, int | None]] = {}
    infos: dict[int, tuple[int, int | None]] = {}

    def walk(start: int, end: int, parent: int | None) -> None:
        pos = start
        while pos < end:
            typ = data[pos:pos + 4]
            size = struct.unpack_from("<I", data, pos + 4)[0]
            if typ == b"GRUP":
                label = data[pos + 8:pos + 12]
                gtype = struct.unpack_from("<i", data, pos + 12)[0]
                if (gtype == 0 and label == b"DIAL") or gtype == 7:  # 7 — ответы темы, в метке — номер темы
                    walk(pos + 24, pos + size, struct.unpack_from("<I", label)[0] & 0xFFFFFF if gtype == 7 else None)
                pos += size
                continue
            if typ in (b"DIAL", b"INFO"):
                flags, form = struct.unpack_from("<II", data, pos + 8)
                body = data[pos + 24:pos + 24 + size]
                if flags & FLAG_COMPRESSED:
                    body = zlib.decompress(body[4:])
                subs: dict[bytes, bytes] = {}
                for sub, payload in iter_subrecords(body):
                    subs.setdefault(sub, payload)
                sid = lambda name: struct.unpack_from("<I", subs[name])[0] if len(subs.get(name, b"")) >= 4 else None
                if typ == b"DIAL":
                    dials[form & 0xFFFFFF] = (subs.get(b"DATA", b"\0\0")[1], sid(b"FULL"))
                elif parent is not None:
                    infos[form & 0xFFFFFF] = (parent, sid(b"RNAM"))
            pos += 24 + size

    walk(24 + struct.unpack_from("<I", data, 4)[0], len(data), None)
    out = {}
    for form, (topic, rnam) in infos.items():
        kind, full = dials.get(topic, (-1, None))
        out[form] = (kind, full, rnam)
    return out


class DialogueTexts:
    """Текст реплики по (файл плагина, номер INFO). Грузится при первом обращении; потокобезопасно."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._lock = threading.Lock()
        self._texts: dict[tuple[str, int], str] | None = None
        self._prompts: dict[tuple[str, int], str] = {}  # (плагин, INFO) -> что сказал игрок перед этим ответом

    def text(self, plugin_file: str, form: int) -> str:
        """Все ответы реплики по порядку; "" — нет такой (реплика мода) или она без текста (вздох, крик)."""
        plugin = Path(plugin_file).stem.lower()
        texts = self._load()
        # Update.esm правит записи Skyrim.esm — его версия приоритетнее
        for source in (["update", plugin] if plugin == "skyrim" else [plugin]):
            if (found := texts.get((source, form & 0xFFFFFF))) is not None:
                return found
        return ""

    def prompt(self, plugin_file: str, form: int) -> str:
        """Что выбрал игрок в меню разговора, если этот ответ NPC — на его реплику; иначе ""."""
        plugin = Path(plugin_file).stem.lower()
        self._load()
        for source in (["update", plugin] if plugin == "skyrim" else [plugin]):
            if found := self._prompts.get((source, form & 0xFFFFFF)):
                return found
        return ""

    def _load(self) -> dict[tuple[str, int], str]:
        with self._lock:
            if self._texts is None:
                self._texts = {}
                try:
                    strings = load_strings(self.data_dir / "Skyrim - Interface.bsa", "ilstrings")
                    for name in PLUGINS:
                        if not (self.data_dir / f"{name}.esm").is_file():
                            continue
                        by_info: dict[int, list[tuple[int, str]]] = {}
                        for (form, number), (_, _, sid) in load_responses(self.data_dir / f"{name}.esm").items():
                            text = strings.get(name, {}).get(sid) or ""
                            if not text and name == "update":  # правки Update.esm ссылаются и на строки Skyrim
                                text = strings.get("skyrim", {}).get(sid, "")
                            text = text.strip()
                            by_info.setdefault(form, []).append((number, text))
                        for form, parts in by_info.items():
                            self._texts[(name, form)] = " ".join(t for _, t in sorted(parts) if t)
                    titles = load_strings(self.data_dir / "Skyrim - Interface.bsa", "strings")
                    for name in PLUGINS:
                        if not (self.data_dir / f"{name}.esm").is_file():
                            continue
                        table = {**titles.get("skyrim", {}), **titles.get(name, {})}
                        for form, (kind, full, rnam) in load_prompts(self.data_dir / f"{name}.esm").items():
                            if kind not in PLAYER_TOPICS:
                                continue
                            # подсказка ответа важнее названия темы (у тем с подсказками оно служебное)
                            text = next((t for t in (table.get(rnam), table.get(full))
                                         if t and not EDITOR_ID.fullmatch(t.strip())), "")
                            if text := TAG.sub("", text).strip():
                                self._prompts[(name, form)] = text
                    log.info("Реплики игры: %d записей, выборов игрока: %d", len(self._texts), len(self._prompts))
                except OSError as e:
                    log.warning("Не прочитать реплики игры (%s) — Гослинг не услышит NPC", e)
            return self._texts
