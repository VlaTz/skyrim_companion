"""Мозг компаньона: когда заговорить самому.

Получает изменения в игре (companion/game/state.py), реплики NPC рядом (подслушанное) и раз в секунду
проверяет тишину. Говорить ли — решает модель: на каждый повод она пишет, что сказала бы, и ставит interest
0–10 (шкала одна для всех поводов); Гослинг говорит, если interest не ниже brain.interest. Модель видит, когда
и что Гослинг сам сказал последним и идёт ли бой, — и сама решает, не болтает ли он подряд. Разговорчивость —
одной цифрой, без пауз и кубиков.

Код только не даёт перебивать себя: пока Гослинг говорит или отвечает игроку, поводы копятся и уходят модели
одной пачкой, когда он освободится; между вопросами модели — не меньше brain.ask_gap секунд (чтобы не
спрашивать её на каждый чих). Подслушанное ждёт, пока NPC договорят и игрок выйдет из меню разговора.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from companion import trace
from companion.config import BrainConfig
from companion.game.state import Change, GameState
from companion.voice.service import VoiceService

log = logging.getLogger(__name__)

# Шкала — общая для событий, подслушанного и тишины; examples — что для этого повода значит «важно вам».
SCALE = ("Оцени, стоит ли это твоей реплики прямо сейчас, — поле interest от 0 до 10 в том же JSON:\n"
         "{bands}"
         "9–10 — нельзя промолчать: впервые увиденное, тяжёлое ранение Владоса, внезапная беда, которую он может "
         "не заметить; обычный бой — не 9–10.\n"
         "Оценивай сам этот повод, а не обстановку в целом. {recency} Если ты только что говорил (меньше минуты "
         "назад) или идёт бой — ставь не выше 6, кроме 9–10: подряд болтать незачем, в бою — не до разговоров.\n"
         "В text — что бы ты сказал: 1–2 коротких предложения от себя, всего не больше 25 слов. А если ставишь "
         "ниже {threshold} — всё равно напиши в text, но не серьёзную реплику, а шутку или подколку к месту одной "
         "фразой, не больше 10 слов (скажешь её не всегда). "
         'Формат: {{"interest": 0..10, "emotion": "...", "text": "..."}} — interest обязателен.')
EVENT_BANDS = ("0–2 — обыденное: вошли в помещение, открыл сундук, подобрал мелочь, очередной бой с обычными "
               "врагами;\n"
               "3–4 — можно бы, но незачем;\n"
               "5–6 — есть что сказать к месту, но можно и промолчать;\n"
               "7–8 — важно или интересно вам: новое задание или цель, знакомый человек, новое место, находка, "
               "враг вдали, поступок Владоса, о котором есть что сказать, мысль, которую стоит сказать сейчас;\n")
OVERHEARD_BANDS = ("0–2 — фон: работа, хозяйство, торг, приветствия, обычная болтовня горожан;\n"
                   "3–4 — любопытно, но не про вас;\n"
                   "5–6 — есть что сказать, но можно и промолчать: чья-то беда или ссора, спор о войне, "
                   "политике, богах;\n"
                   "7–8 — касается вас: ваше задание, его люди и находки (в том числе разговор Владоса с NPC "
                   "о деле), награда, слух о месте или человеке; или по-настоящему смешно;\n")
IDLE_BANDS = ("0–4 — сейчас не до разговоров: бой, напряжённый момент, Владос занят делом;\n"
              "5–6 — сказать совсем нечего;\n"
              "7–8 — долгая тишина сама по себе повод: есть хоть что-то к месту — байка или воспоминание о том, где "
              "вы были, вопрос Владосу, мысль о том, что вокруг;\n")
EVENT_PROMPT = "Игрок ничего не говорил. Только что: {what}.\n"
OVERHEARD_PROMPT = ("Рядом только что говорили (ты слышал):\n{lines}\n"
                    "Оценивай именно эти реплики, а не обстановку в целом; не пересказывай услышанное — "
                    "игрок слышал то же самое.\n")
IDLE_PROMPT = "Вы уже давно идёте молча ({silence}). Можешь заговорить сам.\n"
HEARD_MAX_AGE = 30    # подслушанное старше минуты (Гослинг всё это время был занят) — не комментируем
DIALOGUE_STALE = 60  # меню разговора «открыто», а реплик нет 2 минуты — видимо, пропустили его закрытие
GAME_STALE = 20       # секунд без событий из игры — игра не запущена или висит на загрузке
# Срочное — держится до brain.fresh_important, даже если не помечено важным: тяжело ранен, Крик, впервые увиденное.
URGENT = {"low_health", "power", "novel"}
TICK = 0.25           # с — как часто мозг смотрит, не пора ли (раньше 1 с — до секунды лишней задержки)


class Brain:
    def __init__(self, cfg: BrainConfig, voice: VoiceService, game: GameState):
        self.cfg = cfg
        self.voice = voice
        self.game = game
        self._changes: queue.Queue[Change] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="brain")
        self.last_ask = 0.0                      # когда последний раз спрашивали модель, заговорить ли
        self._idle_checked = 0.0                 # когда последний раз спрашивали её на тишину
        self._commented: dict[str, float] = {}   # место или текст события -> когда показывали модели
        self._heard: list[Change] = []           # подслушанное с прошлого решения
        self._heard_at = 0.0                     # когда пришла последняя реплика NPC
        self._heard_lock = threading.Lock()

    def start(self) -> None:
        self._thread.start()
        log.info("Мозг: сам заговаривает, если модель оценит повод не ниже %d (тишина — %d с)",
                 self.cfg.interest, self.cfg.idle_after)

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
        pending: list[tuple[float, Change]] = []  # (когда пришло, что)
        first_at = 0.0
        while not self._stop.is_set():
            try:
                pending.append((time.time(), self._changes.get(timeout=TICK)))
                first_at = first_at or time.time()
                continue  # сначала забираем всё, что пришло пачкой
            except queue.Empty:
                pass
            now = time.time()
            # Пока Гослинг говорит, поводы ждут — но обстановка уходит: обычное старше brain.fresh_for, важное —
            # старше brain.fresh_important уже не про то, что сейчас («вошли в лавку», а вы уже вышли).
            stale = [c for t, c in pending
                     if now - t > (self.cfg.fresh_important if c.important or c.kind in URGENT else self.cfg.fresh_for)]
            if stale:
                self._silent(stale, "пока был занят, повод устарел")
                pending = [(t, c) for t, c in pending if c not in stale]
                first_at = min((t for t, _c in pending), default=0.0)
            # Связанные события (вошли в город + сменилось помещение) ждём settle секунд и решаем разом.
            if pending and now - first_at >= self.cfg.settle:
                if self._on_changes([c for _t, c in pending], now):
                    pending, first_at = [], 0.0
            elif not pending:
                self._maybe_idle(now)
            self._maybe_overheard(now)

    def _free(self, now: float) -> bool:
        """Можно спросить модель: Гослинг не говорит и не отвечает, и с прошлого вопроса прошло ask_gap."""
        return not self.voice.busy() and now - self.last_ask >= self.cfg.ask_gap

    def _on_changes(self, changes: list[Change], now: float) -> bool:
        """False — Гослинг занят, события стоит придержать."""
        loaded = max((i for i, c in enumerate(changes) if c.kind == "loaded"), default=0)
        if loaded:  # загрузили сейв: что копилось до этого (и мысли), уже про другое прошлое
            del changes[:loaded]
        shown = [c for c in changes if c.kind in self.cfg.events]
        # Одно и то же место недавно уже показывали — не повторяемся.
        fresh = [c for c in shown if now - self._commented.get(c.subject or c.text, 0) > self.cfg.repeat_after]
        if not fresh:
            if shown:
                self._silent(shown, "об этом уже думал недавно")
            return True
        if not self._free(now):
            return False
        for c in fresh:
            self._commented[c.subject or c.text] = now
        what = "; ".join(c.text for c in fresh)
        log.info("Мозг: событие -> %s", what)
        self._ask(EVENT_PROMPT.format(what=what), EVENT_BANDS, now)
        return True

    def _maybe_overheard(self, now: float) -> None:
        with self._heard_lock:
            if not self._heard or now - self._heard_at < self.cfg.overheard_quiet:
                return  # разговор ещё идёт
            if now - self._heard_at > HEARD_MAX_AGE:
                self._heard = []  # так и не дождались удобного момента — уже неактуально
                return
            if self.game.in_dialogue and now - self._heard_at < DIALOGUE_STALE:
                return  # игрок ещё говорит с NPC — не встреваем, ждём конца разговора
            if not self._free(now):
                return  # Гослинг говорит или только что спрашивали — решим чуть позже
            lines, self._heard = self._heard, []
        log.info("Мозг: подслушал %d реплик — встрять ли?", len(lines))
        self._ask(OVERHEARD_PROMPT.format(lines="\n".join(c.text for c in lines)), OVERHEARD_BANDS, now)

    def _maybe_idle(self, now: float) -> None:
        # Тишина — с последней реплики (его или игрока), а не с последнего вопроса «заговорить ли»: в городе мозг
        # спрашивает модель о болтовне стражников каждые несколько секунд, и тишина иначе не наступала никогда.
        anchor = max(self.voice.last_activity, self._idle_checked)
        if now - anchor < self.cfg.idle_after or not self._free(now):
            return
        self._idle_checked = now
        if self.game.updated is None or now - self.game.updated > GAME_STALE:
            return
        log.info("Мозг: долгая тишина")
        self._ask(IDLE_PROMPT.format(silence=f"{max(1, round((now - anchor) / 60))} мин"), IDLE_BANDS, now)

    def _ask(self, situation: str, bands: str, now: float) -> None:
        self.last_ask = now
        scene = self._scene()
        # Неважное (ниже порога) — иногда всё-таки короткой шуткой: не в бою и не сразу после своей реплики.
        llm = self.voice.llm
        quip = (0.0 if self.game.in_combat or (llm and now - llm.last_reply_at < self.cfg.quip_gap)
                else self.cfg.quip_chance)
        self.voice.initiate(situation + SCALE.format(bands=bands, recency=self._recency(now),
                                                     threshold=self.cfg.interest),
                            min_interest=self.cfg.interest, still_fresh=lambda: self._scene() == scene,
                            quip_chance=quip)

    def _scene(self) -> tuple:
        """Обстановка, при смене которой реплика по своей инициативе уже не к месту: место, помещение, бой.
        Пока модель думала и синтезировался голос (~3–4 с), игрок мог выйти из лавки или влезть в драку."""
        s = self.game.status
        return s.location, s.cell, bool(s.in_combat)

    def _recency(self, now: float) -> str:
        """Что модели знать о себе, чтобы решить, не болтает ли она подряд."""
        llm = self.voice.llm
        said = (f"Ты сам говорил {round(now - llm.last_reply_at)} с назад: «{llm.last_reply_text}»."
                if llm and llm.last_reply_at else "Ты пока ничего не говорил.")
        return said + (" Сейчас идёт бой." if self.game.in_combat else "")

    def _silent(self, changes: list[Change], reason: str) -> None:
        """В трассу: почему модель даже не спросили. Только о том, что вообще показываем ей (не о злокрысах)."""
        could = [c.text for c in changes if c.kind in self.cfg.events]
        if could:
            trace.event(f"Мозг промолчал: {'; '.join(could)[:200]} — {reason}")
