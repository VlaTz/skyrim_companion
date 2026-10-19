"""Связывает память с сервисом: по событиям игры открывает память персонажа, пишет журнал и собирает
из памяти текст для модели.

- game_loaded / status с ID персонажа -> открыть его память (и убрать в архив записи «из будущего»);
- изменения в игре (события, подслушанное у NPC) -> журнал;
- реплики игрока и Гослинга (события голосового сервиса) -> журнал;
- открыли память персонажа -> подслушанное за последние минуты обратно в обстановку и on_open (сервис
  восстанавливает историю разговора LLM);
- ушли из места (другое поселение / дикие земли) -> on_place_changed (летописцу: глава кончилась);
- «remember» в ответе Гослинга -> факт о текущем месте;
- meet() — встреча (раса врага, Крик): записать и сказать, сколько раз и где уже встречали;
- game_context() — обстановка «Сейчас в игре» + справка о текущем месте (данные игры) + бывали ли тут +
  что узнал об этом месте и о людях рядом;
- memory_context() — блок {memory} системного промпта: цели, договорённости, что знает о Владосе,
  последние записи дневника, где бывали вместе.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable

from companion.game.books import book_key
from companion.knowledge import stems
from companion.game.places import PlaceBook
from companion.game.state import HEARD_SECONDS, Change, GameEvent, GameState, Heard
from companion.memory.store import MemoryStore, Where, memory_key
from companion.memory.soul import soul_block

log = logging.getLogger(__name__)

MAX_VISITS = 8        # сколько посещённых мест называть в {memory}
DIARY_IN_PROMPT = 3   # последних записей дневника — в {memory}
MAX_FACTS = 8         # фактов о месте и людях рядом — в «Сейчас в игре»
GOAL_TITLES = {"goal": "Цели", "promise": "Договорённости", "player": "Что ты знаешь о Владосе"}
NOW_DAYS = 0.05       # ~1 игровой час: «сейчас здесь», а не «бывали»
# Справка recall: сколько строк из каждого раздела памяти (по порядку вывода) и длина строки журнала.
RECALL_LIMITS = {"дневник": 3, "факты": 4, "цели и договорённости": 2, "журнал": 6}
RECALL_LINE = 220
RECALL_OWN = 2  # своих реплик в ответе — не больше: три его же «Лукан работает у Белетора» модель повторяла
MEET_AGAIN = 600      # с: та же встреча (бой идёт, дракон кружит) — не новая


def days_ago(days: float) -> str:
    if days < NOW_DAYS:
        return "только что"
    if days < 1:
        return f"{max(1, round(days * 24))} ч назад"
    return f"{round(days)} дн. назад"


class MemoryKeeper:
    def __init__(self, store: MemoryStore, game: GameState, places: PlaceBook | None = None):
        self.store = store
        self.game = game
        self.places = places
        self.on_open: Callable[[], None] = lambda: None
        self.on_place_changed: Callable[[], None] = lambda: None
        self._days: float | None = None  # игровая дата из последнего события
        self._area_now: str | None = None  # поселение (или «дикие земли»), где сейчас
        # Сколько раз память открывали заново (другой персонаж, загружен сейв постарше): что надумали раньше
        # (мыслитель) — уже про другое прошлое.
        self.opened = 0
        self.knowledge = None  # companion.knowledge.Knowledge — прочитанные книги в поиск (ставит сервер)
        self.shown_ideas: dict[str, str] = {}  # метка -> мысль, как показаны модели в «Твоих мыслях» последний раз
        self.mood = None  # companion.mood.Mood — настроение в обстановку (ставит сервер)
        self.vision = None  # companion.vision.Vision — что вы видите вокруг (ставит сервер)

    @property
    def area(self) -> str | None:
        """Поселение (или «дикие земли»), где сейчас."""
        return self._area_now

    def where(self) -> Where:
        s = self.game.status
        place = s.location if s.location else ("дикие земли" if s.location == "" else "")
        if s.cell and s.cell != s.location:
            place = f"{place}, {s.cell}" if place else s.cell
        return Where(place, self._days)

    # ------------------------------------------------------------ запись

    def on_game_event(self, event: GameEvent, changes: list[Change]) -> None:
        data = event.data
        if isinstance(data.get("days_passed"), (int, float)):
            self._days = float(data["days_passed"])
        if event.type in ("game_loaded", "status") and (data.get("character") or data.get("player_name")):
            character, name = str(data.get("character") or ""), str(data.get("player_name") or "")
            # status приходит раз в 5 с — открываем, только если это другой персонаж (или сервис перезапущен);
            # game_loaded — всегда: мог загрузиться сейв постарше того же персонажа.
            if event.type == "game_loaded" or memory_key(character, name) != self.store.key:
                if self.store.open(character, name, self._days):
                    self.opened += 1
                    self._restore_heard()
                    self._restore_stats()
                    self._restore_read()
                    self.on_open()
        where = self.where()
        for change in changes:
            self.store.record("heard" if change.kind == "overheard" else "event", change.text, where)
        # Прочитанное: Владос открыл книгу — «читал Владос»; отдал книгу Гослингу — тот прочитал сам.
        if event.type == "book_read":
            self._read(book_key(str(data.get("file", "")), int(data.get("form", 0))), "владос")
        elif event.type == "gosling_items":
            for item in data.get("given") or []:
                if item.get("book"):
                    self._read(book_key(str(item.get("file", "")), int(item.get("form", 0))), "гослинг")
        if event.type == "stat":  # статистика игры — в память персонажа, чтобы привычки пережили перезапуск
            self.store.set_meta("stats", json.dumps(self.game.profile.stats, ensure_ascii=False))
        if event.type == "status":
            location = self.game.status.location
            area = None if location is None else (self._area(location) if location else "дикие земли")
            if area and self._area_now and area != self._area_now:
                self.on_place_changed()
            self._area_now = area or self._area_now

    def on_voice_event(self, event: dict) -> None:
        """События голосового сервиса: player_speech, companion_reply."""
        if event.get("type") == "player_speech":
            self.store.record("player", str(event.get("text", "")), self.where())
        elif event.get("type") == "companion_reply":
            where = self.where()
            self.store.record("gosling", str(event.get("text", "")), where,
                              emotion=event.get("emotion"), initiative=bool(event.get("initiative")))
            if remember := str(event.get("remember") or "").strip():
                self.store.add_fact(self._area_now or "разное", remember, "запомнил в разговоре", where)
                log.info("Память: запомнил — %s", remember)

    def meet(self, category: str, key: str) -> tuple[int, str] | None:
        """Встреча (category «раса» | «крик», key «дракон»): записать. -> (сколько раз встречали раньше, где и когда
        последний раз). None — не считаем: память не открыта или это продолжение той же встречи."""
        if not self.store.is_open or not key:
            return None
        past = self.store.last_encounters(category, key)
        if past and time.time() - past[0][0] < MEET_AGAIN:
            return None
        self.store.add_encounter(category, key, self.where())
        if not past:
            log.info("Память: впервые — %s: %s", category, key)
            return 0, ""
        _real, days, place = past[0]
        when = days_ago(self._days - days) if days is not None and self._days is not None else ""
        return len(past), ", ".join(x for x in (place, when) if x)

    def _restore_stats(self) -> None:
        try:
            stats = json.loads(self.store.get_meta("stats") or "{}")
        except json.JSONDecodeError:
            stats = {}
        for name, value in stats.items():
            self.game.profile.stat(name, value)

    def _read(self, key: str, who: str) -> None:
        """Книгу прочитали (who — владос | гослинг): в память персонажа и в поиск знаний."""
        if self.store.is_open:
            self.store.add_encounter(f"книга:{who}", key, self.where())
        if self.knowledge:
            self.knowledge.add_read(key, who)

    def _restore_read(self) -> None:
        """Память открыли заново (сейв постарше, другой персонаж): в поиске — книги, прочитанные в этой ветке."""
        if not self.knowledge:
            return
        read = {key: "владос" for key in self.store.last_by_key("книга:владос")}
        read |= {key: "гослинг" for key in self.store.last_by_key("книга:гослинг")}  # сам читал — главнее
        self.knowledge.set_read(read)

    def _restore_heard(self) -> None:
        """Подслушанное за последние минуты — обратно в «Ты слышал рядом» (сервис мог быть перезапущен)."""
        restored = [h for at, text in self.store.recent("heard", HEARD_SECONDS) if (h := Heard.parse(text, at))]
        self.game.heard.clear()
        self.game.heard.extend(restored)
        if restored:
            log.info("Память: подслушанное за последние минуты — %d реплик", len(restored))

    # ------------------------------------------------------------ чтение (текст для модели)

    def _area(self, name: str) -> str:
        """Лавка, таверна... -> поселение, в котором она («Товары Белетора» -> «Вайтран»)."""
        place = self.places.find(name) if self.places else None
        town = self.places.settlement_of(place) if place else None
        return town.name if town else name

    def visits(self) -> list[tuple[str, float, float]]:
        """Где бывали — по поселениям: (место, первая и последняя игровая дата), свежие первыми."""
        merged: dict[str, tuple[float, float]] = {}
        for name, first, last in self.store.visits():
            area = self._area(name)
            was = merged.get(area)
            merged[area] = (min(first, was[0]), max(last, was[1])) if was else (first, last)
        return sorted(((n, f, l) for n, (f, l) in merged.items()), key=lambda v: -v[2])

    def game_context(self) -> str:
        """«Сейчас в игре»: обстановка + что Гослинг знает о текущем месте."""
        parts = [self.game.describe()]
        if self.mood:  # настроение — из того, что было: говорит в нём, а не беззаботен после гибели Владоса
            parts.append(self.mood.describe())
        if self.vision and (seen := self.vision.describe()):  # вид — то, чего нет в данных игры
            parts.append(seen)
        location = self.game.status.location
        card = self.places.card(location) if self.places and location else ""
        if card:
            parts.append("Справка о текущем месте (ты знаешь это как местный; place_info для него не нужен): "
                         + card)
        if location and self._days is not None:
            area = self._area(location)
            for name, first, _last in self.visits():
                if name == area and self._days - first > NOW_DAYS:
                    parts.append(f"Вы уже бывали здесь вместе: впервые {days_ago(self._days - first)}.")
                    if before := self._place_diary(area):  # память о месте при входе — не только «бывали»
                        parts.append(before)
                    break
        if facts := self._facts_here(location):
            parts.append("Что ты сам узнал об этом месте и людях рядом:\n" + "\n".join(facts))
        # Мысли — только надуманные здесь же: «Адрианна совсем рядом» после ухода из Вайтрана — не к месту.
        ideas = self.store.ideas() if (self.store.get_meta("ideas_area") or "") == (self._area_now or "") else []
        # С метками: высказал мысль хоть в ответе Владосу — модель пишет её метку ("idea"), и мысль помечается
        # сказанной (Thinker.said). Без этого «шахту» из мыслей он предлагал в ответах, а в тишине — снова (16.10).
        self.shown_ideas = {f"m{i}": idea for i, idea in enumerate(ideas, 1)}
        if ideas:
            parts.append("Твои мысли (можешь высказать, когда к месту, своими словами; что уже сказал — не повторяй; "
                         "высказал — метку в \"idea\"):\n"
                         + "\n".join(f"- [{key}] {idea}" for key, idea in self.shown_ideas.items()))
        return "\n".join(p for p in parts if p)

    def _facts_here(self, location: str | None) -> list[str]:
        """Факты о текущем поселении (и местах в нём) и о людях рядом — свежие, не больше MAX_FACTS."""
        area = self._area(location) if location else None
        people = {n.name for n in self.game.status.nearby or []}
        # Упоминание в тексте тоже считается: факт «коготь Лукана унесли в Ветреный пик» записан про Ветреный пик,
        # но при Лукане рядом он к месту. Человека узнаём и по имени («Лукан» из «Лукан Валерий»).
        names = {w for n in people for w in [n, n.split()[0]] if len(w) >= 4} | ({area} if area else set())

        def relevant(subject: str, text: str) -> bool:
            return (subject in people or (area is not None and self._area(subject) == area)
                    or any(n in text for n in names))

        found = [f"- {subject}: {text}" + (f" ({source})" if source else "")
                 for subject, text, source in self.store.facts() if relevant(subject, text)]
        return found[-MAX_FACTS:]

    # ------------------------------------------------------------ вспоминание

    def recall(self, query: str) -> str:
        """Справка recall: что было в вашем общем прошлом по запросу — дневник, факты, цели (и закрытые), журнал
        (реплики, события, подслушанное). Всё — этой ветки сейва. Основы слов запроса — как в базе знаний; выше —
        больше совпавших слов, при равенстве — свежее."""
        if not self.store.is_open:
            return "Память ещё не открыта — вспомнить нечего."
        words = stems(query)
        if not words:
            return "Не понял, что вспомнить: назови место, человека, вещь или дело."
        pats = [re.compile(r"(?<!\w)" + re.escape(w)) for w in words]

        def hits(*texts: str | None) -> int:
            low = " ".join(t for t in texts if t).casefold()
            return sum(bool(p.search(low)) for p in pats)

        def when(days: float | None, place: str | None) -> str:
            ago = days_ago(self._days - days) if days is not None and self._days is not None else "?"
            return f"{ago}, {place}" if place else ago

        found: list[tuple[int, float, str, str]] = []  # (совпало слов, дата, раздел, строка)
        for days, place, text in self.store.all_diary():
            if n := hits(text, place):
                found.append((n, days or 0, "дневник", f"[{when(days, place)}] {text}"))
        for subject, text, source in self.store.facts():
            if n := hits(subject, text):
                found.append((n, 0, "факты", f"{subject}: {text}" + (f" ({source})" if source else "")))
        for days, kind, text, status in self.store.all_goals():
            if n := hits(text):
                state = {"done": "сделано", "dropped": "отменено"}.get(status, "в силе")
                found.append((n, days or 0, "цели и договорённости", f"{text} ({state})"))
        for days, kind, text, place in self.store.journal_all():
            if n := hits(text):
                # Свои реплики — не факт: Гослинг мог ошибиться («Лукан работает в «Товарах Белетора»»), а найденное
                # модель повторяет. Помечаем и ставим ниже остального при равных совпадениях.
                who = {"player": f"{self.game.player_name}: «{text}»", "gosling": f"ты говорил (мог ошибаться): «{text}»",
                       "heard": f"слышал: {text}"}.get(kind, text)
                found.append((n - 0.5 * (kind == "gosling"), days or 0, "журнал",
                              f"[{when(days, place)}] {who[:RECALL_LINE]}"))
        if not found:
            return (f"О «{query}» в твоей памяти ничего нет — этого не было или ты не помнишь. Так и скажи, "
                    "не выдумывай.")
        found.sort(key=lambda f: (-f[0], -f[1]))
        out: dict[str, list[str]] = {}
        own = 0
        for _n, _d, part, line in found:
            mine = "ты говорил (мог ошибаться)" in line
            if len(out.setdefault(part, [])) < RECALL_LIMITS[part] and not (mine and own >= RECALL_OWN):
                out[part].append(line)
                own += mine
        return ("Что ты помнишь (ваше общее прошлое по памяти; чего тут нет — того ты не помнишь, не выдумывай):\n"
                + "\n".join(f"{part}:\n" + "\n".join(f"- {line}" for line in lines)
                            for part in RECALL_LIMITS if (lines := out.get(part))))

    def _place_diary(self, area: str) -> str:
        """Что было в этом поселении в прошлый раз — последняя запись дневника о нём (не о нынешнем визите)."""
        if self._days is None:
            return ""
        for days, place, text in reversed(self.store.all_diary()):
            if place and days is not None and self._days - days > NOW_DAYS and self._area(place.split(", ")[0]) == area:
                return f"Что было здесь в прошлый раз ({days_ago(self._days - days)}, твой дневник): {text}"
        return ""

    def memory_context(self) -> str:
        """{memory} системного промпта. Пусто — память ещё не открыта или вспомнить нечего."""
        if self._days is None:
            return ""
        parts = []
        if profile := self.game.profile.summary(self.game.player_name):
            parts.append(profile)
        if soul := soul_block(self.store):  # что Гослинг решил о себе — о себе говорит в согласии с этим
            parts.append(soul)
        goals = self.store.active_goals()
        for kind, title in GOAL_TITLES.items():
            items = [text for _gid, k, text in goals if k == kind]
            if items:
                parts.append(f"{title}: " + "; ".join(items) + ".")
        diary = self.store.recent_diary(DIARY_IN_PROMPT)
        if diary:
            parts.append("Твой дневник (последние записи):\n" + "\n".join(
                f"- {days_ago(self._days - days) if days is not None else '?'}, {place or '?'}: {text}"
                for days, place, text in diary))
        visits = [f"{name} — {days_ago(self._days - last)}"
                  for name, _first, last in self.visits()[:MAX_VISITS]]
        if visits:
            parts.append("Где вы бывали вместе (по игровому времени): " + "; ".join(visits) + ".")
        if parts:
            parts.append("Чего нет в памяти и дневнике, ты не помнишь — подробностей не выдумывай.")
        return "\n".join(parts)
