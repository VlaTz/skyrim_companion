"""Предметы игры по названию: оружие, броня, еда и зелья, ингредиенты — из Skyrim.esm и DLC (русские строки).

Нужны, чтобы в душе Гослинга (companion/memory/soul.py) не закреплялись выдуманные вещи («рагу из медведя с
картошкой» — такого в Скайриме нет): конкретный предмет в душе — только существующий в игре. Сверка — по основам слов
(как в базе знаний): «медовуха чёрный вереск» найдёт «Медовуха «Черный вереск»».
"""

from __future__ import annotations

import logging
import re
import struct
import threading
from pathlib import Path

from companion.game.books import plugin_records
from companion.game.dialogue import PLUGINS
from companion.game.gamefiles import load_strings

log = logging.getLogger(__name__)

KINDS = {b"WEAP", b"ARMO", b"ALCH", b"INGR"}
SIMILAR = 6  # похожих настоящих предметов — в подсказку


def words(text: str) -> list[str]:
    """Основы слов: нижний регистр, ё -> е, окончание отрезано (как stems() базы знаний, но без стоп-слов)."""
    out = []
    for w in re.findall(r"[\w'-]+", text.lower().replace("ё", "е")):
        if len(w) >= 3:
            out.append(w if len(w) <= 4 else w[:max(4, len(w) - 2)])
    return out


class ItemBook:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._names: list[tuple[str, list[str]]] | None = None  # (название, основы слов)
        self._lock = threading.Lock()

    def _load(self) -> list[tuple[str, list[str]]]:
        with self._lock:
            if self._names is None:
                names: set[str] = set()
                try:
                    strings = load_strings(self.data_dir / "Skyrim - Interface.bsa", "strings")
                    for plugin in PLUGINS:
                        path = self.data_dir / f"{plugin}.esm"
                        if not path.is_file():
                            continue
                        for _typ, _form, subs in plugin_records(path, KINDS):
                            if subs.get(b"FULL"):
                                sid = struct.unpack_from("<I", subs[b"FULL"][0])[0]
                                name = (strings.get(plugin, {}).get(sid) or strings.get("skyrim", {}).get(sid) or "")
                                if name.strip():
                                    names.add(name.strip())
                except OSError as e:
                    log.warning("Предметы: не прочитать файлы игры (%s)", e)
                self._names = [(n, words(n)) for n in sorted(names, key=len)]
                log.info("Предметы игры: %d названий", len(self._names))
            return self._names

    def exists(self, mention: str) -> bool:
        """Есть ли в игре такой предмет: каждая основа слова из упоминания — в начале слова названия (только так:
        наоборот «медведя» находил «мёд»)."""
        want = words(mention)
        if not want:
            return True
        return any(all(any(w.startswith(m) for w in have) for m in want)
                   for _name, have in self._load())

    def similar(self, mention: str, limit: int = SIMILAR) -> list[str]:
        """Настоящие предметы, похожие по словам, — короткие (без зачарований) первыми."""
        want = set(words(mention))
        scored = []
        for name, have in self._load():
            hits = sum(any(w.startswith(m) for w in have) for m in want)
            if hits:
                scored.append((-hits, len(name), name))
        return [name for _h, _l, name in sorted(scored)[:limit]]
