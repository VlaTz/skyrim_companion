"""Мозг компаньона: когда заговорить самому.

Получает изменения в игре (companion/game/state.py) и раз в секунду проверяет тишину.
Решает, стоит ли дать слово модели: шанс по типу события, паузы между репликами,
в бою — молчит (там звучат родные боевые реплики голоса), кроме тяжёлого ранения игрока.
Что именно сказать и говорить ли вообще — решает модель (пустой ответ = промолчать).

Реплики NPC рядом (подслушанное) копятся отдельно: решаем, когда разговор стих и игрок вышел из меню
разговора. Встревает Гослинг редко — только если важно, смешно или задело; это решает модель.
"""

from __future__ import annotations

import logging
import queue
import random
import threading
import time

from companion.config import BrainConfig
from companion.game.state import Change, GameState
from companion.voice.service import VoiceService

log = logging.getLogger(__name__)

EVENT_PROMPT = ("Игрок ничего не говорил. Только что: {what}. Если есть что сказать к месту — "
                "скажи от себя, 1–2 коротких предложения. Если нечего — пустой text.")
IDLE_PROMPT = ("Вы уже давно идёте молча. Можешь заговорить сам: байка, воспоминание о том, где вы были, "
               "мысль о месте, времени суток или погоде, вопрос игроку. 1–2 коротких предложения. "
               "Если не хочется — пустой text.")
OVERHEARD_PROMPT = ("Рядом только что говорили (ты слышал):\n{lines}\n"
                    "Встревай, только если правда есть что сказать: это важно для вас (задание, награда, опасность, "
                    "слух о месте или человеке), это смешно, или задело тебя самого. Чаще всего — промолчи "
                    "(пустой text). Если говоришь — 1–2 коротких предложения от себя; не пересказывай услышанное: "
                    "игрок слышал то же самое.")
DIALOGUE_STALE = 120  # меню разговора «открыто», а реплик нет 2 минуты — видимо, пропустили его закрытие
GAME_STALE = 20  # секунд без событий из игры — игра не запущена или висит на загрузке
MAX_WAIT = 30    # сколько событие может ждать конца паузы, потом уже неактуально


class Brain:
    def __init__(self, cfg: BrainConfig, voice: VoiceService, game: GameState):
        self.cfg = cfg
        self.voice = voice
        self.game = game
        self._changes: queue.Queue[Change] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="brain")
        self.last_initiative = 0.0               # когда последний раз заговорил сам
        self._idle_checked = 0.0                 # когда последний раз бросали кубик на тишину
        self._commented: dict[str, float] = {}   # место или текст события -> когда комментировали
        self._heard: list[Change] = []           # подслушанное с прошлого решения
        self._heard_at = 0.0                     # когда пришла последняя реплика NPC
        self._heard_lock = threading.Lock()
        self._overheard_checked = 0.0            # когда последний раз решали, встревать ли

    def start(self) -> None:
        self._thread.start()
        log.info("Мозг: сам заговаривает (пауза %d с, тишина %d с)", self.cfg.min_gap, self.cfg.idle_after)

    def stop(self) -> None:
        self._stop.set()

    def notify(self, changes: list[Change]) -> None:
        """Вызывается из обработчика /game/event."""
        for change in changes:
            if change.kind == "overheard":
                with self._heard_lock:
                    self._heard.append(change)
                    self._heard_at = time.time()
            else:
                self._changes.put(change)

    # ------------------------------------------------------------ цикл

    def _run(self) -> None:
        pending: list[Change] = []
        first_at = 0.0
        while not self._stop.is_set():
            try:
                pending.append(self._changes.get(timeout=1.0))
                first_at = first_at or time.time()
                continue  # сначала забираем всё, что пришло пачкой
            except queue.Empty:
                pass
            now = time.time()
            # Связанные события (вошли в город + сменилось помещение) ждём settle секунд и решаем разом.
            # Не вышла пауза после прошлой реплики — события ждут её, но не дольше MAX_WAIT.
            if pending and now - first_at >= self.cfg.settle:
                if self._on_changes(pending) or now - first_at > MAX_WAIT:
                    pending, first_at = [], 0.0
            elif not pending:
                self._maybe_idle(now)
            self._maybe_overheard(now)

    def _on_changes(self, changes: list[Change]) -> bool:
        """False — рано (не вышла пауза после прошлой реплики), события стоит придержать."""
        now = time.time()
        urgent = any(c.kind == "low_health" for c in changes)
        if self.game.in_combat and not urgent:
            return True
        gap = self.cfg.urgent_gap if urgent else self.cfg.min_gap
        if now - self.last_initiative < gap:
            return False
        # Одно и то же место недавно уже комментировали — не повторяемся.
        fresh = [c for c in changes
                 if now - self._commented.get(c.subject or c.text, 0) > self.cfg.repeat_after]
        triggers = [c for c in fresh if random.random() < self.cfg.chances.get(c.kind, 0.0)]
        if not triggers:
            return True
        what = "; ".join(c.text for c in changes)
        log.info("Мозг: событие -> %s", what)
        self.last_initiative = now
        for c in changes:
            self._commented[c.subject or c.text] = now
        self.voice.initiate(EVENT_PROMPT.format(what=what))
        return True

    def _maybe_overheard(self, now: float) -> None:
        with self._heard_lock:
            if not self._heard or now - self._heard_at < self.cfg.overheard_quiet:
                return  # разговор ещё идёт
            if self.game.in_dialogue and now - self._heard_at < DIALOGUE_STALE:
                return  # игрок ещё говорит с NPC — не встреваем, ждём конца разговора
            lines, self._heard = self._heard, []
        # Не время — этот разговор просто пропускаем: он уже в «Ты слышал рядом» (ответит, если спросят).
        if (self.game.in_combat or now - self._overheard_checked < self.cfg.overheard_gap
                or now - self.last_initiative < self.cfg.min_gap
                or random.random() >= self.cfg.chances.get("overheard", 0.0)):
            return
        self._overheard_checked = now
        log.info("Мозг: подслушал %d реплик — встрять ли?", len(lines))
        if self.voice.initiate(OVERHEARD_PROMPT.format(lines="\n".join(c.text for c in lines))):
            self.last_initiative = time.time()

    def _maybe_idle(self, now: float) -> None:
        anchor = max(self.voice.last_activity, self.last_initiative, self._idle_checked)
        if now - anchor < self.cfg.idle_after:
            return
        self._idle_checked = now
        if self.game.updated is None or now - self.game.updated > GAME_STALE or self.game.in_combat:
            return
        if random.random() >= self.cfg.idle_chance:
            return
        log.info("Мозг: долгая тишина")
        self.last_initiative = now
        self.voice.initiate(IDLE_PROMPT)
