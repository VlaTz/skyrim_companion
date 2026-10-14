"""Журнал квестов игрока — для инструментов агента («куда нам идти?», «что там по квесту?»).

Плагин присылает активные квесты: название, тип, этап, текущие цели, номер строки последней записи
журнала и имена для подстановок (<Alias=Target> -> «Арнгейр»). Текст записи журнала берём из строк
игры (Skyrim - Interface.bsa, *_russian.dlstrings) — так плагину не нужно вызывать функции игры.
"""

from __future__ import annotations

import logging
import re
import threading
import time
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


MISC_TYPES = (0, 6)  # «прочее», «разное»: поручения вроде «отнести письмо» — не важное событие


class Quest(BaseModel):
    id: str
    name: str
    type: int = 0
    stage: int = 0
    objectives: list[str] = []
    aliases: dict[str, str] = {}
    log: QuestLogRef | None = None
    tracked: bool = False  # игрок отметил задание активным в журнале (маркер на компасе)

    @property
    def kind(self) -> str:
        return QUEST_TYPES.get(self.type, "квест")


class QuestLog:
    def __init__(self, interface_bsa: Path):
        self.bsa = interface_bsa
        self._lock = threading.Lock()
        self.quests: list[Quest] = []
        self._strings: dict[str, dict[int, str]] | None = None  # грузим при первом обращении
        self._baseline = False  # был ли журнал после загрузки сейва (с чем сравнивать)
        self._touched: dict[str, float] = {}  # id задания -> когда менялись его цели
        self._gone: dict[str, Quest] = {}     # пропавшие из журнала (выполнены или на миг без целей)

    def reset(self) -> None:
        """Загрузили сейв: следующий журнал — точка отсчёта, без событий."""
        with self._lock:
            self._baseline = False
            self._gone.clear()

    def update(self, data: dict) -> list[tuple[str, str, str, bool]]:
        """Новый журнал -> что изменилось: [(вид, текст, о чём, важное)].
        Новое задание, новая цель, цель выполнена (пропала из текущих), задание завершено (пропало из журнала)."""
        quests = [Quest.model_validate(q) for q in data.get("quests", [])]
        now = time.time()
        with self._lock:
            old = {q.id: q for q in self.quests}
            self.quests = quests
            if not self._baseline:
                self._baseline = True
                return []
        changes = []

        def add(q: Quest, text: str) -> None:
            misc = q.type in MISC_TYPES
            changes.append(("quest_misc" if misc else "quest", text, f"задание: {q.name}", not misc))
            self._touched[q.id] = now

        new_ids = {q.id for q in quests}
        for q in quests:
            goals = [self._expand(o, q) for o in q.objectives]
            # Пропало и вернулось (на смене этапа игра на миг снимает все цели) — не «новое», а новые цели.
            was = old.get(q.id) or self._gone.pop(q.id, None)
            if not was:
                add(q, f"новое задание «{q.name}»" + (": " + "; ".join(goals) if goals else ""))
                continue
            before = [self._expand(o, was) for o in was.objectives]
            for goal in before:
                if goal not in goals:
                    add(q, f"цель выполнена («{q.name}»): {goal}")
            for goal in goals:
                if goal not in before:
                    add(q, f"новая цель («{q.name}»): {goal}")
        # completed — id заданий журнала, которые игра считает выполненными (новый плагин); без него — по пропаже.
        completed = data.get("completed")
        for q in old.values():
            if q.id not in new_ids:
                self._gone[q.id] = q
                if completed is not None and q.id not in completed:
                    continue  # не выполнено — просто на миг без целей (или провалено) — молчим
                goals = "; ".join(self._expand(o, q) for o in q.objectives)
                add(q, f"задание «{q.name}» завершено" + (f" (последняя цель: {goals})" if goals else ""))
        return changes

    def context(self, limit: int, fresh: float) -> str:
        """Текущие цели самых нужных заданий — для «Сейчас в игре»: отмеченное игроком, менявшееся недавно,
        главный квест, потом остальные не-поручения."""
        now = time.time()
        with self._lock:
            quests = list(self.quests)

        def rank(q: Quest) -> tuple:
            recent = now - self._touched.get(q.id, 0) < fresh
            return (not q.tracked, not recent, q.type != 1, q.type in MISC_TYPES, -self._touched.get(q.id, 0))

        picked = [q for q in sorted(quests, key=rank) if q.objectives][:limit]
        if not picked:
            return ""
        lines = [f"- «{q.name}» ({q.kind}" + (", отмечено игроком" if q.tracked else "") + "): "
                 + "; ".join(self._expand(o, q) for o in q.objectives) for q in picked]
        more = len(quests) - len(picked)
        return ("Задания — текущие цели из журнала игрока (запись журнала — quest_details"
                + (f"; ещё заданий: {more} — quests" if more > 0 else "") + "):\n" + "\n".join(lines))

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
