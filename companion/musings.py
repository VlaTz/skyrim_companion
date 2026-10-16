"""Случайные размышления о мире: в долгой тишине Гослингу приходит мысль о Скайриме — «ты вот никогда не думал,
почему…» — и он рассуждает вслух, если тема зацепила.

Повод — из данных игры, не из памяти модели: запись «общеизвестного» базы знаний (экраны загрузки). Чаще — связанная
с местом, где вы сейчас (brain.musings.local), иначе — любая. Сказанные темы помнятся по сейву (encounters,
категория «размышление»), как сказанные шутки. Говорить ли — решает модель (пустой text — промолчал); запрос — с
рассуждением модели (brain.musings.reasoning): в тишине ответа никто не ждёт.
"""

from __future__ import annotations

import hashlib
import logging
import random

from companion.knowledge import Knowledge
from companion.memory.keeper import MemoryKeeper

log = logging.getLogger(__name__)

SAID = "размышление"  # категория в памяти встреч


def topic_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


class Musings:
    def __init__(self, knowledge: Knowledge, memory: MemoryKeeper, local_share: float = 0.5):
        self.knowledge = knowledge
        self.memory = memory
        self.local_share = local_share
        self._skipped: set[str] = set()  # темы, на которые модель промолчала, — в этот запуск больше не предлагаем

    def topic(self, place: str = "") -> str | None:
        """Тема для размышления: ещё не сказанная в этой ветке сейва; к месту — с шансом local_share."""
        said = set(self.memory.store.last_by_key(SAID)) if self.memory.store.is_open else set()
        fresh = lambda texts: [t for t in texts if topic_key(t) not in said and topic_key(t) not in self._skipped]  # noqa: E731
        if place and random.random() < self.local_share:
            if local := fresh(self.knowledge.lore(place)):
                return random.choice(local)
        pool = fresh(self.knowledge.lore())
        return random.choice(pool) if pool else None

    def said(self, text: str) -> None:
        if self.memory.store.is_open:
            self.memory.store.add_encounter(SAID, topic_key(text), self.memory.where())
        log.info("Размышление сказано: %s", text[:80])

    def skipped(self, text: str) -> None:
        self._skipped.add(topic_key(text))
