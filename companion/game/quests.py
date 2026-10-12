"""Журнал квестов игрока — для инструментов агента («куда нам идти?», «что там по квесту?»).

Плагин присылает активные квесты: название, тип, этап, текущие цели, номер строки последней записи
журнала и имена для подстановок (<Alias=Target> -> «Арнгейр»). Текст записи журнала берём из строк
игры (Skyrim - Interface.bsa, *_russian.dlstrings) — так плагину не нужно вызывать функции игры.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from pydantic import BaseModel

from companion.game.gamefiles import load_strings

log = logging.getLogger(__name__)

QUEST_TYPES = {
    0: "прочее", 1: "главный квест", 2: "Коллегия Винтерхолда", 3: "Гильдия воров", 4: "Тёмное Братство",
    5: "Соратники", 6: "разное", 7: "даэдрический квест", 8: "побочный квест", 9: "гражданская война",
    10: "Стража Рассвета", 11: "Драконорождённый",
}
ALIAS_RE = re.compile(r"<Alias(?:\.\w+)*=([^>]+)>", re.I)
TAG_RE = re.compile(r"<[^>]+>")  # прочие теги (<Global=…>, разметка) — убираем


class QuestLogRef(BaseModel):
    id: int
    files: list[str] = []  # где искать строку: файл последней правки квеста, затем исходный


class Quest(BaseModel):
    id: str
    name: str
    type: int = 0
    stage: int = 0
    objectives: list[str] = []
    aliases: dict[str, str] = {}
    log: QuestLogRef | None = None

    @property
    def kind(self) -> str:
        return QUEST_TYPES.get(self.type, "квест")


class QuestLog:
    def __init__(self, interface_bsa: Path):
        self.bsa = interface_bsa
        self._lock = threading.Lock()
        self.quests: list[Quest] = []
        self._strings: dict[str, dict[int, str]] | None = None  # грузим при первом обращении

    def update(self, data: dict) -> None:
        quests = [Quest.model_validate(q) for q in data.get("quests", [])]
        with self._lock:
            self.quests = quests

    def _expand(self, text: str, quest: Quest) -> str:
        text = ALIAS_RE.sub(lambda m: quest.aliases.get(m.group(1), "(?)"), text)
        return TAG_RE.sub("", text).strip()

    def _journal(self, quest: Quest) -> str:
        if not quest.log:
            return ""
        if self._strings is None:
            try:
                self._strings = load_strings(self.bsa, "dlstrings")
            except OSError as e:
                log.warning("Нет строк игры для журнала (%s): %s", self.bsa, e)
                self._strings = {}
        for file in quest.log.files:
            table = self._strings.get(Path(file).stem.lower(), {})
            if text := table.get(quest.log.id):
                return self._expand(text, quest)
        return ""

    def summary(self) -> str:
        """Инструмент quests(): все активные квесты с текущими целями."""
        with self._lock:
            quests = list(self.quests)
        if not quests:
            return "Активных квестов в журнале нет."
        lines = []
        for q in sorted(quests, key=lambda q: (q.type != 1, q.name)):  # главный квест — первым
            goals = "; ".join(self._expand(o, q) for o in q.objectives) or "цель не указана"
            lines.append(f"«{q.name}» ({q.kind}) — сейчас: {goals}")
        return "\n".join(lines)

    def details(self, name: str) -> str:
        """Инструмент quest_details(name): цели и последняя запись журнала квеста."""
        with self._lock:
            quests = list(self.quests)
        name = name.strip().lower()
        # По названию, а если не нашлось — по типу («главный» -> главный квест).
        matches = ([q for q in quests if name and name in q.name.lower()]
                   or [q for q in quests if name and name in q.kind.lower()])
        if not matches:
            names = ", ".join(f"«{q.name}»" for q in quests) or "нет"
            return f"Квеста «{name}» нет среди активных. Активные: {names}."
        q = matches[0]
        lines = [f"«{q.name}» ({q.kind})"]
        if q.objectives:
            lines.append("Текущие цели: " + "; ".join(self._expand(o, q) for o in q.objectives))
        if journal := self._journal(q):
            lines.append(f"Запись в журнале: {journal}")
        return "\n".join(lines)
