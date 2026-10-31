"""Мысли Гослинга: фоновым запросом к модели — что ему держать в голове и, может быть, предложить Владосу.

Свои реплики Гослинг говорит без справок (быстро) и видит только «Сейчас в игре» — сам не догадается, что у
Владоса полная сумка железа, а Белетор за углом. Мыслитель смотрит шире: обстановка с карточкой места, сумка
целиком, задания, память (цели, договорённости, что знает о Владосе), последние реплики и прежние мысли — и
возвращает 0–3 мысли. Они хранятся в памяти (goals, kind idea — по сейву, как всё) и видны модели в «Сейчас в игре».
Когда сказать, решает сам мыслитель (when): now — новая мысль сразу идёт мозгу, и тот говорит её без переоценки;
later — при случае: мозг высказывает её на долгую тишину (unsaid/said); never — заметка для себя.

Думает, когда что-то поменялось: другое поселение, задания (цели), сумка (на SUM_CHANGE вещей) — не чаще
thinker.min_gap; без перемен — раз в thinker.quiet_every, пока игра идёт. Настройки — секция thinker
в config.yaml (ThinkerConfig).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from companion import naming
from companion.config import ThinkerConfig
from companion.game.items import words
from companion.game.state import Change, GameState
from companion.memory.keeper import MemoryKeeper

log = logging.getLogger(__name__)

GAME_STALE = 20       # с без событий из игры — игра не идёт, думать не о чем
SAID_WINDOW = 3600    # с — высказанные за этот срок мыслитель видит (было «10 последних» — через 20 минут забывал)
SAID_MAX = 40         # и не больше стольких
# Метка «idea» в ответе: модель путает номера (после высказанной мысли список «Твоих мыслей» сдвигается, а ответ уже
# идёт со старыми метками — 9 меток из 28 за час стояли у мыслей, о которых в реплике ни слова) и не ставит её у
# реплики, что почти пересказывает мысль. Сходство — доля характерных основ мысли (слова от 5 букв), что есть в реплике.
MARK_MIN = 0.15       # метке верим, если реплика хоть немного похожа на мысль
GUESS_MIN = 0.6       # метка ложная или её нет, а реплика почти пересказывает мысль из списка — это она


def distinct(text: str) -> set[str]:
    return {w for w in words(text) if len(w) >= 5}


def resemblance(idea: str, reply: str) -> float:
    """Какая доля характерных основ мысли есть в реплике (0..1)."""
    mine = distinct(idea)
    return len(mine & distinct(reply)) / len(mine) if mine else 0.0


def ago_minutes(seconds: float) -> str:
    return "только что" if seconds < 90 else f"{round(seconds / 60)} мин назад"

SYSTEM = """Ты — внутренний голос Гослинга, спутника Владоса в Скайриме: весёлый молодой норд, бывший картограф.
По обстановке, сумке, заданиям и памяти реши, что Гослингу сейчас стоит держать в голове и, может быть,
предложить Владосу при случае. Практичное: продать лишнее, где тут что купить, подлечиться или отдохнуть, что
дальше по заданию и где это, по пути ли что-то из целей и договорённостей. И личное: вспомнить к месту общее
прошлое, спросить Владоса о чём-то, высказать своё мнение.
Каждая мысль — одна короткая фраза: что и почему именно сейчас (с именами и местами из данных). Только то, что
следует из данных, — ничего не выдумывай о мире. Не повторяй то, что Гослинг уже предлагал в последних репликах,
и не предлагай того, от чего Владос отказался или что ему не нравится. Мысль из прежних ещё к месту — оставь её.
Текущую цель задания («Айрилет ждёт у башни») не пересказывай — Владос и так видит её в журнале; про задание —
только то, чего в цели нет: где это, по пути ли, что взять с собой.
Ничего толкового — пустой список.
Отвечай строго одним JSON-объектом без пояснений и markdown:
{"thoughts": [{"text": "...", "new": true, "when": "now"}, ...]} — от 0 до 3 мыслей; new — true, если этой мысли
не было среди прежних (прежняя другими словами или с уточнением — false); when — когда сказать её Владосу вслух:
"now" — сейчас, потом будет поздно или не к месту (лавка прямо тут, а сумка полна; по пути к цели — то, что
нельзя пропустить); "later" — при случае, когда долго идёте молча; "never" — заметка для себя (что учесть,
о чём пока молчать) и то, что Гослинг уже сказал вслух."""


class Thinker:
    def __init__(self, keeper: MemoryKeeper, game: GameState, complete: Callable[..., dict | None],
                 on_thought: Callable[[list[Change]], None] = lambda changes: None, cfg: ThinkerConfig | None = None):
        self.cfg = cfg or ThinkerConfig()
        self.keeper = keeper
        self.store = keeper.store
        self.game = game
        c = self.cfg  # своя модель мыслителя (thinker.model): мысли заменяются каждый раз, ошибка живёт недолго
        self.complete = lambda system, user, **kw: complete(system, user, model=c.model or None, reasoning=c.reasoning,
                                                            max_tokens=c.max_tokens or None, **kw)
        self.on_thought = on_thought  # новые мысли — мозгу: повод заговорить
        self._last_run = 0.0
        self._seen: tuple | None = None  # (поселение, цели заданий, сумка) при прошлом раздумье
        self._epoch = -1  # keeper.opened при прошлом раздумье: память открыли заново (запуск, загрузка сейва)
        self._here: str | None = None  # где были при прошлой проверке
        self._left: dict[str | None, float] = {}  # место -> когда ушли оттуда (вернулись быстро — не думаем заново)
        self._busy = threading.Lock()
        # Мысли последнего раздумья, которые стоит сказать вслух (when now/later), и где надуманы; что уже сказано.
        self._speak: list[str] = []
        self._speak_at: tuple[int, str | None] = (-1, None)  # (keeper.opened, keeper.area)
        self._said: set[str] = set()

    def unsaid(self) -> str | None:
        """Мысль, которую Гослинг ещё не сказал вслух, — мозгу на долгую тишину. Только надуманная здесь же и в этой
        ветке сейва: «Адрианна совсем рядом» после ухода из Вайтрана не к месту."""
        if self._speak_at != (self.keeper.opened, self.keeper.area):
            return None
        return next((t for t in self._speak if t not in self._said), None)

    def spoken(self, label: str | None, reply: str, shown: dict[str, str]) -> list[str]:
        """Какие мысли из показанных (метка -> мысль) реплика высказала: по метке модели, если реплика на мысль
        похожа; иначе — самая похожая, если почти пересказывает её. Остальное — никакая."""
        by_label = shown.get(str(label or "").strip("[] "))
        if by_label and resemblance(by_label, reply) >= MARK_MIN:
            return [by_label]
        best = max(shown.values(), key=lambda idea: resemblance(idea, reply), default=None)
        if best and resemblance(best, reply) >= GUESS_MIN:
            log.info("Мысль «%s»: метка %s, а реплика пересказывает другую — помечаю её", best[:60], label or "не стоит")
            return [best]
        if by_label:
            log.info("Мысль «%s»: метка %s не про эту реплику — не помечаю", by_label[:60], label)
        return []

    def said(self, text: str) -> None:
        """Мозг высказал мысль — больше её не предлагать: из «Твоих мыслей» в обстановке — вон (в памяти — «said»),
        а мыслитель видит её в «уже сказал вслух» и не придумывает заново другими словами."""
        self._said.add(text)
        self.store.mark_idea_said(text)

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
        """Что поменялось с прошлого раздумья (пусто — ничего). Вернулись туда, откуда ушли меньше thinker.revisit
        назад (граница локации: «вошли — ушли — вошли»), — не перемена: место запоминаем без раздумья."""
        area, quests, bag = now
        old_area, old_quests, old_bag = self._seen
        if area and area != old_area:
            if time.time() - self._left.get(area, 0.0) < self.cfg.revisit:
                log.info("Мыслитель: вернулись в «%s» меньше %d с назад — заново не думаю", area, self.cfg.revisit)
                self._seen = (area, old_quests, old_bag)
            else:
                return f"другое место: {area}"
        if quests != old_quests:
            return "задания"
        if sum(abs(bag.get(k, 0) - old_bag.get(k, 0)) for k in bag.keys() | old_bag.keys()) >= self.cfg.bag_change:
            return "сумка"
        return ""

    def check(self, force: bool = False) -> bool:
        """Подумать, если пора. True — подумал."""
        now = time.time()
        # Гослинг не рядом с Владосом — не думает о месте, где его нет: что поменялось, увидит, когда вы снова вместе.
        if (not self.store.is_open or self.game.updated is None or now - self.game.updated > GAME_STALE
                or self.game.apart_since is not None or not self._busy.acquire(blocking=False)):
            return False
        try:
            state = self._state()
            if state[0] != self._here:  # сменилось место — помним, когда ушли из прежнего
                self._left[self._here] = now
                self._here = state[0]
            if self.keeper.opened != self._epoch:
                # Запуск сервиса или загрузка сейва: мысли этой ветки — в памяти (в обстановке «Твои мысли»), заново не
                # думаем — ждём перемен (место, задания, сумка) или thinker.quiet_every.
                self._seen, self._last_run, self._said, self._epoch = state, now, set(), self.keeper.opened
                self._speak, self._left = [], {}
                log.info("Мыслитель: запуск или загрузка — мысли из памяти, заново не думаю")
                return False
            why = self._why(state) or ("давно не думал" if now - self._last_run >= self.cfg.quiet_every else "")
            if not force and (not why or now - self._last_run < self.cfg.min_gap):
                return False
            self._seen, self._last_run, self._epoch = state, now, self.keeper.opened
            return self._think(why or "вручную")
        finally:
            self._busy.release()

    # ------------------------------------------------------------ раздумье

    def _think(self, why: str) -> bool:
        now = time.time()
        old = self.store.ideas()
        name = naming.CANON  # имя Игрока — из конфига, подставится в запросе (companion/naming.py)
        dialogue = [f"{name}: {text}" if kind == "player" else f"Гослинг: {text}"
                    for kind, text, _emotion, _initiative in self.store.last_dialogue(self.cfg.dialogue_lines)
                    if kind in ("player", "gosling")][-self.cfg.dialogue_lines:]
        user = ("Сейчас в игре:\n" + (self.keeper.game_context() or "нет данных")
                + "\n\nСумка Владоса подробно:\n" + self.game.player_details()
                + "\n\nВсе задания из журнала:\n" + self.game.quest_log.summary()
                + "\n\nПамять Гослинга:\n" + (self.keeper.memory_context() or "пока ничего")
                + "\n\nПоследние реплики:\n" + ("\n".join(dialogue) or "пока не разговаривали")
                + "\n\nПрежние мысли:\n" + ("\n".join(f"- {i}" + (" (уже сказал вслух)" if i in self._said else "")
                                                      for i in old) or "нет")
                + "\n\nУже сказал вслух (когда) — не повторяй и не переформулируй, пока Владос сам не вернётся к этому:\n"
                + ("\n".join(f"- {i} ({ago_minutes(now - at)})" for i, at in self.store.said_ideas(SAID_MAX, SAID_WINDOW))
                   or "ничего"))
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
        for t in thoughts:
            t["text"] = str(t["text"]).strip()
            # Прежний формат (say) и мусор: say false — заметка для себя, непонятное «когда» — при случае.
            t["when"] = "never" if t.get("say") is False else str(t.get("when") or "later")
            if t["when"] not in ("now", "later", "never"):
                t["when"] = "later"
        ideas = [t["text"] for t in thoughts]
        self.store.replace_ideas(ideas, self.keeper.where(), area or "")
        self._speak = [t["text"] for t in thoughts if t["when"] != "never"]
        self._speak_at = (epoch, area)
        log.info("Мыслитель (%s): %s", why, "; ".join(f"{t['text']} [{t['when']}]" for t in thoughts) or "ничего")
        # Мозгу сразу — только новые и срочные; «при случае» он возьмёт сам на тишину (unsaid). Прежняя мысль
        # другими словами или заметка для себя («про продажу пока не напоминать») — не повод заговорить.
        now = [t["text"] for t in thoughts
               if t["when"] == "now" and t.get("new") is not False and t["text"] not in old]
        if now:
            self.on_thought([Change("thought", f"тебе пришла мысль: {i}", subject=f"мысль: {i}", important=True)
                             for i in now])
        return True
