"""Мысли Гослинга: фоновым запросом к модели — что ему держать в голове и, может быть, предложить Владосу.

Свои реплики Гослинг говорит без справок (быстро) и видит только «Сейчас в игре» — сам не догадается, что у
Владоса полная сумка железа, а Белетор за углом. Мыслитель смотрит шире: обстановка с карточкой места, сумка
целиком, задания, память (цели, договорённости, что знает о Владосе), последние реплики и прежние мысли — и
возвращает 0–3 мысли. Они хранятся в памяти (goals, kind idea — по сейву, как всё) и видны модели в «Сейчас в игре»;
новая мысль — повод мозгу: модель сама оценит, высказать ли её сейчас.

Думает, когда что-то поменялось: другое поселение, задания (цели), сумка (на SUM_CHANGE вещей) — не чаще
thinker.min_gap; без перемен — раз в thinker.quiet_every, пока игра идёт. Настройки — секция thinker
в config.yaml (ThinkerConfig).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from companion.config import ThinkerConfig
from companion.game.state import Change, GameState
from companion.memory.keeper import MemoryKeeper

log = logging.getLogger(__name__)

GAME_STALE = 20       # с без событий из игры — игра не идёт, думать не о чем

SYSTEM = """Ты — внутренний голос Гослинга, спутника Владоса в Скайриме: весёлый молодой норд, бывший картограф.
По обстановке, сумке, заданиям и памяти реши, что Гослингу сейчас стоит держать в голове и, может быть,
предложить Владосу при случае. Практичное: продать лишнее, где тут что купить, подлечиться или отдохнуть, что
дальше по заданию и где это, по пути ли что-то из целей и договорённостей. И личное: вспомнить к месту общее
прошлое, спросить Владоса о чём-то, высказать своё мнение.
Каждая мысль — одна короткая фраза: что и почему именно сейчас (с именами и местами из данных). Только то, что
следует из данных, — ничего не выдумывай о мире. Не повторяй то, что Гослинг уже предлагал в последних репликах,
и не предлагай того, от чего Владос отказался или что ему не нравится. Мысль из прежних ещё к месту — оставь её.
Ничего толкового — пустой список.
Отвечай строго одним JSON-объектом без пояснений и markdown:
{"thoughts": [{"text": "...", "new": true, "say": true}, ...]} — от 0 до 3 мыслей; new — true, если этой мысли
не было среди прежних (прежняя другими словами или с уточнением — false); say — true, если её стоит при случае
сказать Владосу вслух, false — заметка для себя (что учесть, о чём пока молчать)."""


class Thinker:
    def __init__(self, keeper: MemoryKeeper, game: GameState, complete: Callable[..., dict | None],
                 on_thought: Callable[[list[Change]], None] = lambda changes: None, cfg: ThinkerConfig | None = None):
        self.cfg = cfg or ThinkerConfig()
        self.keeper = keeper
        self.store = keeper.store
        self.game = game
        self.complete = complete
        self.on_thought = on_thought  # новые мысли — мозгу: повод заговорить
        self._last_run = 0.0
        self._seen: tuple | None = None  # (поселение, цели заданий, сумка) при прошлом раздумье
        self._epoch = -1  # keeper.opened при прошлом раздумье: память открыли заново — думать заново
        self._busy = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="thinker").start()
        log.info("Мыслитель: думает при смене места, заданий, сумки (не чаще %d с) и раз в %d мин",
                 self.cfg.min_gap, self.cfg.quiet_every // 60)

    def _run(self) -> None:
        while True:
            time.sleep(self.cfg.check_every)
            try:
                self.check()
            except Exception:
                log.exception("Мыслитель: ошибка")

    # ------------------------------------------------------------ когда думать

    def _state(self) -> tuple:
        quests = tuple(sorted((q.name, tuple(q.objectives)) for q in self.game.quest_log.quests))
        bag = {category: info.get("total", 0) for category, info in (self.game.player.get("bag") or {}).items()}
        return self.keeper.area, quests, bag

    def _why(self, now: tuple) -> str:
        """Что поменялось с прошлого раздумья (пусто — ничего)."""
        if self._seen is None:
            return "начало"
        area, quests, bag = now
        old_area, old_quests, old_bag = self._seen
        if area and area != old_area:
            return f"другое место: {area}"
        if quests != old_quests:
            return "задания"
        if sum(abs(bag.get(k, 0) - old_bag.get(k, 0)) for k in bag.keys() | old_bag.keys()) >= self.cfg.bag_change:
            return "сумка"
        return ""

    def check(self, force: bool = False) -> bool:
        """Подумать, если пора. True — подумал."""
        now = time.time()
        if (not self.store.is_open or self.game.updated is None or now - self.game.updated > GAME_STALE
                or not self._busy.acquire(blocking=False)):
            return False
        try:
            state = self._state()
            if self.keeper.opened != self._epoch:  # загрузили сейв (или другой персонаж) — всё с начала
                self._seen, self._last_run = None, 0.0
            why = self._why(state) or ("давно не думал" if now - self._last_run >= self.cfg.quiet_every else "")
            if not force and (not why or now - self._last_run < self.cfg.min_gap):
                return False
            self._seen, self._last_run, self._epoch = state, now, self.keeper.opened
            return self._think(why or "вручную")
        finally:
            self._busy.release()

    # ------------------------------------------------------------ раздумье

    def _think(self, why: str) -> bool:
        old = self.store.ideas()
        name = self.store.player_name or "Владос"
        dialogue = [f"{name}: {text}" if kind == "player" else f"Гослинг: {text}"
                    for kind, text, _emotion, _initiative in self.store.last_dialogue(self.cfg.dialogue_lines)
                    if kind in ("player", "gosling")][-self.cfg.dialogue_lines:]
        user = ("Сейчас в игре:\n" + (self.keeper.game_context() or "нет данных")
                + "\n\nСумка Владоса подробно:\n" + self.game.player_details()
                + "\n\nВсе задания из журнала:\n" + self.game.quest_log.summary()
                + "\n\nПамять Гослинга:\n" + (self.keeper.memory_context() or "пока ничего")
                + "\n\nПоследние реплики:\n" + ("\n".join(dialogue) or "пока не разговаривали")
                + "\n\nПрежние мысли:\n" + ("\n".join(f"- {i}" for i in old) or "нет"))
        epoch, area = self.keeper.opened, self.keeper.area
        result = self.complete(SYSTEM, user, title=f"Мыслитель: {why}")
        if self.keeper.area != area:
            # Пока думал (Sonnet — несколько секунд), ушли в другое место: мысли про прошлое — подумать заново.
            log.info("Мыслитель: пока думал, сменилось место (%s -> %s) — мысли выброшены", area, self.keeper.area)
            self._last_run = 0.0
            return False
        if self.keeper.opened != epoch:
            # Пока думал, загрузили сейв: мысли — про прошлое, которого в этой ветке нет («дракон убит», а бой впереди).
            log.info("Мыслитель: пока думал, память открыли заново — мысли выброшены")
            self._epoch = -1
            return False
        if not isinstance(result, dict) or not isinstance(result.get("thoughts"), list):
            log.warning("Мыслитель: модель не вернула мысли — попробуем позже")
            return False
        thoughts = [t if isinstance(t, dict) else {"text": t} for t in result["thoughts"]]
        thoughts = [t for t in thoughts if str(t.get("text", "")).strip()][:self.cfg.max_ideas]
        ideas = [str(t["text"]).strip() for t in thoughts]
        self.store.replace_ideas(ideas, self.keeper.where(), area or "")
        # Мозгу — только новые и те, что стоит сказать вслух: прежняя мысль другими словами или заметка для себя
        # («про продажу пока не напоминать») — не повод заговорить.
        new = [str(t["text"]).strip() for t in thoughts
               if t.get("new") is not False and t.get("say") is not False and str(t["text"]).strip() not in old]
        log.info("Мыслитель (%s): %s", why, "; ".join(ideas) or "ничего")
        if new:
            self.on_thought([Change("thought", f"тебе пришла мысль: {i}", subject=f"мысль: {i}") for i in new])
        return True
