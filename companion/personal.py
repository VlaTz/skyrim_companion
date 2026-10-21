"""Личные реплики Гослинга: событие задело то, что он о себе решил (душа) — повод заговорить от себя.

Настроение и так сдвигается от поводов души (companion/mood.py), но реплику это не запускало: «боюсь великанов» —
а на великана он реагировал так же, как на любого врага. Теперь событие, задевшее запись души, — отдельный повод мозгу
(Change kind personal): «это задевает тебя лично — по твоей душе «…»»; мозг говорит от себя по обычной шкале интереса
(companion/brain.py, PERSONAL_TOUCH) — «не к месту» всё так же вправе промолчать. Общий механизм, без частных случаев:
записи души и их поводы пишет сама душа (companion/memory/soul.py), а у новой локации — ещё и название места в тексте
записи («прошлое: …в Солитьюде» — вошли в Солитьюд).

Чтобы не твердил одно и то же: одну запись души — не чаще personal.gap, любую — не чаще personal.gap_all.
Подарок и «забрал» не берём: на них Гослинг и так отвечает лично (brain.PERSONAL), с настроением от души.
Настройки — секция personal.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from companion.config import PersonalConfig
from companion.game.state import Change
from companion.mood import Mood

log = logging.getLogger(__name__)

SKIP = {"gift", "taken", "personal", "thought", "loaded", "died"}  # на эти и так отвечает лично, или это не событие мира
FEELING_TEXT = {"радость": "тебя это радует", "страх": "тебя это пугает", "неприязнь": "тебе это не по душе",
                "грусть": "тебе от этого грустно", "воспоминание": "это место из твоего прошлого"}


class Personal:
    def __init__(self, mood: Mood, cfg: PersonalConfig | None = None, clock: Callable[[], float] = time.time):
        self.cfg = cfg or PersonalConfig()
        self.mood = mood
        self.clock = clock
        self._lock = threading.Lock()
        self._at: dict[str, float] = {}  # ключ записи души -> когда последний раз был повод
        self._any = 0.0

    def notify(self, changes: list[Change]) -> list[Change]:
        """События игры -> поводы «лично» для мозга (пусто — ничего не задело или рано)."""
        now = self.clock()
        out: list[Change] = []
        with self._lock:
            for change in changes:
                if change.kind in SKIP:
                    continue
                for key, text, feeling, trigger in self.mood.touched(change):
                    if now - self._any < self.cfg.gap_all or now - self._at.get(key, 0.0) < self.cfg.gap:
                        log.info("Лично: «%s» задело «%s», но недавно уже было — молчим", change.gist[:60], key)
                        continue
                    self._at[key], self._any = now, now
                    what = (f"это задевает тебя лично ({FEELING_TEXT.get(feeling, feeling)}: «{trigger}») — по твоей "
                            f"душе: «{text}»")
                    log.info("Лично: «%s» -> %s", change.gist[:60], key)
                    out.append(Change("personal", what, subject=f"лично: {key}", important=True))
        return out
