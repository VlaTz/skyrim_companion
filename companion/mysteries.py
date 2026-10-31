"""Загадки: «швы» игры, которые Гослинг видит изнутри мира и не понимает, — Владос погибает и снова жив, сам Гослинг
падает без сил и встаёт, время проваливается на часы, люди слово в слово повторяют одно и то же, в сумку Владоса
влезает невозможное. Он не знает про сохранения и механику игры: для него это странности мира, и о них можно
удивляться, гадать и спрашивать Владоса.

Детекторы — по данным игры, без модели (события и статус плагина). Странность — событие oddity мозгу, но не каждый
раз: в первый раз — удивиться и спросить, на третий — своя догадка (как догадка, не факт), дальше — не чаще
mysteries.gap; в остальное время только счётчик. Разгадал (Владос объяснил — поле "solved" ответа) — больше не
спрашивает: в памяти остаётся ответ, и Гослинг говорит в согласии с ним.

Загадки — в памяти персонажа (meta «mysteries»): через загрузки сохранений Гослинг их помнит — это и есть загадка
(он помнит, как Владос погиб, а тот жив). Блок «Загадки» — в памяти промпта (MemoryKeeper.memory_context).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

from companion import trace
from companion.config import MysteriesConfig
from companion.game.state import Change
from companion.memory.store import MemoryStore

log = logging.getLogger(__name__)

# Загадка: ключ -> (как называется, что увидел — в событие; {n} — который раз, {what} — подробности)
KINDS = {
    "возвращения Владоса": ("Владос погибает — и снова жив",
                            "ты помнишь, как Владос погиб, а он снова жив и стоит там, где вы были раньше, будто этого "
                            "не было{what}"),
    "мои подъёмы": ("ты падаешь без сил — и встаёшь", "тебя почти добили, ты упал без сил — и вот снова на ногах{what}"),
    "провалы во времени": ("время проваливается на часы",
                           "прошло {what}, а ты не помнишь, как оно прошло"),
    "одинаковые слова": ("люди слово в слово повторяют одно и то же", "{what}"),
    "сумка Владоса": ("в сумку Владоса влезает невозможное", "{what}"),
}
FIRST = ("Такое ты видишь впервые и не понимаешь, как это возможно: удивись по-своему и, если к месту, спроси "
         "Владоса. Объяснений как факт не выдумывай — ты не знаешь.")
GUESS = ("Это уже {n}-й раз. Выскажи свою догадку — как догадку, не как факт, — или спроси Владоса, в чём тут дело.")
AGAIN = ("Это уже {n}-й раз, а разгадки нет. Можешь коротко вернуться к своей загадке — не повторяя прежних слов.")
SCORE = " Оцени interest не ниже 7."  # загадку стадия уже отобрала — шкала интереса её не глушит
GUESS_AT = 3            # с какого раза — догадка
REPEAT_GAP = 600        # с — одна и та же реплика NPC: повтором считаем, если прошло больше (а не эхо в ту же минуту)
REPEATS_KEPT = 300      # реплик NPC в счётчике повторов (свежих)
TIMESCALE = 20          # игровых секунд на реальную (по умолчанию в Skyrim)


def _words(text: str) -> set[str]:
    """Слова без знаков и коротких связок: «Владос погибает — и снова жив» -> {владос, погибает, снова, жив}."""
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) > 2}


class Mysteries:
    def __init__(self, store: MemoryStore, cfg: MysteriesConfig | None = None):
        self.cfg = cfg or MysteriesConfig()
        self.store = store
        self._lock = threading.Lock()
        self._clock: tuple[float, float] | None = None  # (игровые дни, реальное время) прошлого статуса
        self._down = False                               # Гослинг лежит без сил
        self._bag_said = 0                               # сколько вещей было, когда говорил о сумке

    # ------------------------------------------------------------ данные игры

    def on_event(self, event_type: str, data: dict, changes: list[Change], player: dict | None) -> list[Change]:
        """Событие игры (как его прислал плагин) и что из него вышло — странности, о которых пора сказать."""
        if not self.cfg.enabled or not self.store.is_open:
            return []
        found: list[tuple[str, str]] = []  # (загадка, подробности)
        if event_type == "game_loaded":
            self._clock, self._down = None, False  # загрузка — не «провал во времени»: время в сейве своё
        for c in changes:
            if c.kind == "died":  # загрузились после гибели Владоса
                found.append(("возвращения Владоса", ""))
            elif c.kind == "overheard" and (what := self._repeat(c)):
                found.append(("одинаковые слова", what))
        if event_type == "status":
            gosling = data.get("gosling") or {}
            if "bleedout" in gosling and not gosling.get("dead"):
                if self._down and not gosling["bleedout"]:
                    found.append(("мои подъёмы", ""))
                self._down = bool(gosling["bleedout"])
            if (jump := self._time_jump(data)) is not None:
                found.append(("провалы во времени", jump))
        if event_type == "player" and (bag := self._bag(player or {})):
            found.append(("сумка Владоса", bag))
        return [c for key, what in found if (c := self._seen(key, what))]

    def _time_jump(self, data: dict) -> str | None:
        """Игровое время ушло вперёд намного быстрее реального: сон, ожидание, быстрое перемещение."""
        days = data.get("days_passed")
        if not isinstance(days, (int, float)):
            return None
        now, clock = time.time(), self._clock
        self._clock = (float(days), now)
        if not clock:
            return None
        hours = (days - clock[0]) * 24
        expected = (now - clock[1]) * TIMESCALE / 3600
        if hours - expected < self.cfg.time_jump_hours:
            return None
        whole = round(hours)
        return f"{whole} ч" if whole < 24 else f"{round(hours / 24, 1)} дн."

    def _repeat(self, change: Change) -> str:
        """Реплику NPC слово в слово слышал уже не раз (в разные дни, а не эхом): чья, что, сколько раз."""
        speaker, _, text = change.subject.partition(": ")
        if not text or speaker == "игрок":
            return ""
        # Счётчик — в памяти персонажа: повторы слышны через дни игры и перезапуски сервиса.
        now = time.time()
        with self._lock:
            try:
                lines = json.loads(self.store.get_meta("heard_repeats") or "{}")
            except json.JSONDecodeError:
                lines = {}
            count, last = lines.get(text, (0, 0.0))
            if now - last < REPEAT_GAP:
                return ""
            count += 1
            lines[text] = (count, now)
            if len(lines) > REPEATS_KEPT:
                lines = dict(sorted(lines.items(), key=lambda kv: kv[1][1])[-REPEATS_KEPT:])
            self.store.set_meta("heard_repeats", json.dumps(lines, ensure_ascii=False))
        if count < self.cfg.repeat_lines:
            return ""
        return f"{speaker} слово в слово сказал то же, что ты слышал уже {count} раз: «{text}»"

    def _bag(self, player: dict) -> str:
        """Вещей у Владоса столько, что столько не унести; снова — когда прибавилось ещё bag_more."""
        bag = player.get("bag") or {}
        total = sum(int(info.get("total", 0)) for info in bag.values() if isinstance(info, dict))
        if total < self.cfg.bag_items or total - self._bag_said < self.cfg.bag_more:
            return ""
        self._bag_said = total
        return f"у Владоса в сумке {total} вещей — столько человеку не унести, а он идёт, как налегке"

    # ------------------------------------------------------------ загадки

    def _load(self) -> dict:
        try:
            return json.loads(self.store.get_meta("mysteries") or "{}")
        except json.JSONDecodeError:
            return {}

    def _save(self, data: dict) -> None:
        self.store.set_meta("mysteries", json.dumps(data, ensure_ascii=False))

    def _seen(self, key: str, what: str) -> Change | None:
        """Увидел странность: счётчик; пора сказать — событие мозгу, иначе молча."""
        with self._lock:
            data = self._load()
            m = data.setdefault(key, {"count": 0, "said": 0.0, "answer": ""})
            m["count"] += 1
            n, now = m["count"], time.time()
            stage = ("" if m["answer"] else FIRST if n == 1 else GUESS.format(n=n) if n == GUESS_AT
                     else AGAIN.format(n=n) if n > GUESS_AT and now - m["said"] >= self.cfg.gap else "")
            if stage:
                m["said"] = now
            self._save(data)
        title, seen = KINDS[key]
        text = seen.format(what=what)
        log.info("Загадка «%s»: %d-й раз%s", key, n, "" if stage else " — молча")
        trace.event(f"Загадка «{key}»: {n}-й раз" + ("" if stage else (" — разгадана, молча" if m["answer"]
                                                                       else " — молча")))
        if not stage:
            return None
        return Change("oddity", f"странность ({title}): {text}. {stage}{SCORE}", subject=f"загадка: {key}",
                      important=True, brief=f"странность: {title}")

    def solve(self, label: str, answer: str) -> bool:
        """Разгадал: Владос объяснил (поле "solved" ответа: «загадка: как»). Больше не спрашивает."""
        # Модель называет загадку то названием («возвращения Владоса»), то заголовком из блока «Загадки» («Владос
        # погибает и снова жив», 29.10 — и разгадка не находилась): сверяем с обоими, без тире, кавычек и регистра.
        said = _words(label)
        with self._lock:
            data = self._load()
            names = {k: _words(k) | _words(KINDS.get(k, ("", ""))[0]) for k in data}
            match = max((k for k in data if said and len(said & names[k]) >= min(2, len(said))),
                        key=lambda k: len(said & names[k]), default=None)
            if not match or not answer.strip():
                return False
            data[match]["answer"] = answer.strip()
            self._save(data)
        log.info("Загадка «%s» разгадана: %s", match, answer)
        trace.event(f"Загадка «{match}» разгадана: {answer}")
        return True

    def describe(self) -> str:
        """Блок «Загадки» в память промпта: что видел и не понимает, что уже разгадано."""
        data = self._load() if self.store.is_open else {}
        if not data:
            return ""
        # Разгаданное — отдельно: в одном списке с пометкой «разгадано» модель его не замечала и снова называла загадкой
        # (29.10: «почему ты умираешь и снова стоишь тут?» — после «я Довакин, боги возвращают»).
        open_ = [f"- «{key}» — {KINDS.get(key, (key, ''))[0]}; видел {m['count']} раз"
                 for key, m in data.items() if not m["answer"]]
        solved = [f"- «{key}» — {KINDS.get(key, (key, ''))[0]}: {m['answer']}" for key, m in data.items() if m["answer"]]
        parts = []
        if open_:
            parts.append("Загадки (странности мира, которые ты видел сам и не понимаешь):\n" + "\n".join(open_))
        if solved:
            parts.append("Уже не загадки — ты знаешь ответ и так и думаешь (не удивляйся и не спрашивай об этом, не "
                         "называй загадкой):\n" + "\n".join(solved))
        return "\n".join(parts)
