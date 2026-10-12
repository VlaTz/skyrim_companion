"""Состояние игры по событиям от SKSE-плагина (или tools/fake_game.py).

Плагин шлёт POST /game/event:
    {"type": "status", "data": {...}}   — снимок состояния раз в несколько секунд
    {"type": "<событие>", "data": {...}} — разовые события (бой, смерть, загрузка ...)

Из снимков GameState сам замечает изменения (сменилась локация, упало здоровье)
и превращает всё в текст {game_context} для LLM.

Поля status (все необязательные):
    location   — локация ("Вайтран", "Ветреный пик"); "" — дикие земли (вне поселений и подземелий)
    hold       — владение ("Вайтран"); "" — неизвестно, остаётся прежнее
    cell       — ячейка ("Гарцующая кобыла"); "" — без названия (обычно улица)
    Строковые поля плагин шлёт всегда: "" значит «нет», отсутствие поля — «не изменилось».
    interior   — внутри помещения
    hour       — игровое время, часы 0..24 (14.5 = 14:30)
    weather    — погода ("ясно", "снег")
    health     — здоровье игрока 0..1
    in_combat  — игрок в бою
    sneaking   — крадётся
    level      — уровень игрока
    pos        — [x, y] игрока на карте мира (только на улице; в помещении не шлётся)
    gosling    — {"loaded", "distance" (м), "follower"}: где NPC Гослинг (если Gosling.esp подключён)
    nearby     — [{"name": "Назим", "hostile": false, "dead": false, "follower": false}, ...]
                 follower — спутник игрока; это тело Гослинга в игре, в «Рядом» его не показываем

Разовые события:
    game_loaded     — {}  (загрузка сохранения)
    combat_started  — {"enemies": ["Драугр-воитель", ...]}
    combat_ended    — {}
    actor_killed    — {"name": "Волк", "by_player": true}
    item_picked     — {"name": "Двемерская шестерня", "count": 1}
    map_markers     — {"worldspace": "Скайрим", "markers": [{"name", "type", "x", "y", "visible", "travel"}]}
                      все маркеры карты; шлётся при загрузке, смене мира и открытии нового места
    quests          — {"quests": [{"id", "name", "type", "stage", "objectives", "aliases", "log"}]}
                      активные квесты журнала (companion/game/quests.py); при загрузке и смене этапа/цели
    player          — {"gold", "level", "health", "magicka", "stamina", "worn", "bag", "effects"}
                      подробности об игроке (инструмент player_details); при смене инвентаря/экипировки
    say_result      — {"id", "ok", "reason"}: Гослинг произнёс реплику (или почему нет) — для голоса из NPC
    npc_line        — {"speaker", "file", "form", "category", "subtype", "to_player"}: NPC рядом (~25 м) начал
                      реплику; текст — по файлу плагина и номеру INFO (companion/game/dialogue.py).
                      to_player — игрок в это время в меню разговора (скорее всего, говорят с ним)
    dialogue_menu   — {"open"}: игрок открыл/закрыл меню разговора с NPC
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from companion.config import MapConfig
from companion.game.dialogue import DialogueTexts
from companion.game.geo import Marker, describe_surroundings, find_places
from companion.game.quests import QuestLog

LOW_HEALTH = 0.3
RECENT_SECONDS = 600  # «недавно» — события за последние 10 минут
WILD_CONFIRM = 8      # секунд вне локации, чтобы засчитать уход: на границе игра дёргает локацию туда-сюда
HEARD_SECONDS = 300   # реплики NPC рядом помним 5 минут (для ответа на «что он сказал?»)
HEARD_REPEAT = 10     # та же реплика того же NPC за 10 с — повтор события, не новая


@dataclass(frozen=True)
class Change:
    """Что изменилось в игре. kind — для мозга, text — для промпта и журнала."""
    kind: str  # loaded, location, interior, low_health, combat_started, combat_ended, killed, item, overheard, other
    text: str
    subject: str = ""  # о чём событие (место) — чтобы не комментировать одно и то же дважды


class GameEvent(BaseModel):
    type: str
    data: dict[str, Any] = {}


class Nearby(BaseModel):
    name: str
    hostile: bool = False
    dead: bool = False
    follower: bool = False


@dataclass(frozen=True)
class Heard:
    """Реплика NPC, которую слышал Гослинг."""
    at: float  # time.time()
    speaker: str
    text: str
    to_player: bool  # говорили с игроком (он был в меню разговора)

    def __str__(self) -> str:
        return f"{self.speaker}{' (игроку)' if self.to_player else ''}: «{self.text}»"


class GoslingStatus(BaseModel):
    loaded: bool = False
    distance: float = -1  # метров до игрока; -1 — не загружен
    follower: bool = False


class Status(BaseModel):
    location: str | None = None
    hold: str | None = None
    cell: str | None = None
    interior: bool | None = None
    hour: float | None = None
    weather: str | None = None
    health: float | None = None
    in_combat: bool | None = None
    sneaking: bool | None = None
    level: int | None = None
    pos: list[float] | None = None
    gosling: GoslingStatus | None = None
    nearby: list[Nearby] | None = None


def time_of_day(hour: float) -> str:
    h = int(hour) % 24
    part = ("ночь" if h < 5 else "утро" if h < 12 else "день" if h < 17 else "вечер" if h < 22 else "ночь")
    return f"{part}, {h:02d}:{int(hour % 1 * 60):02d}"


class GameState:
    """Последнее известное состояние игры + недавние события. Потокобезопасно."""

    def __init__(self, map_cfg: MapConfig | None = None, interface_bsa: Path | None = None, recent: int = 15,
                 dialogue: DialogueTexts | None = None):
        self._lock = threading.Lock()
        self.map_cfg = map_cfg or MapConfig()
        self.quest_log = QuestLog(interface_bsa or Path("Skyrim - Interface.bsa"))
        self.markers: list[Marker] = []
        self.map_world: str | None = None
        self.status = Status()
        self.recent: deque[tuple[float, str]] = deque(maxlen=recent)  # (time.time(), что случилось)
        self.updated: float | None = None  # time.time() последнего события
        self.last_place: str | None = None  # откуда ушли в дикие земли
        self._left: tuple[float, str] | None = None  # (когда, откуда) — уход ещё не подтверждён
        self.player: dict = {}  # подробности об игроке (событие player)
        self.on_say_result: Callable[[dict], None] = lambda data: None  # голос из NPC ждёт подтверждения
        self.dialogue = dialogue  # тексты реплик игры; None — реплики NPC не слышим
        self.heard: deque[Heard] = deque(maxlen=12)
        self.in_dialogue = False  # игрок в меню разговора с NPC

    def apply(self, event: GameEvent) -> list[Change]:
        """Учитывает событие; возвращает, что изменилось."""
        with self._lock:
            self.updated = time.time()
            if event.type == "status":
                changes = self._apply_status(Status.model_validate(event.data))
            elif event.type == "map_markers":
                changes = self._apply_markers(event.data)
            elif event.type == "quests":
                self.quest_log.update(event.data)
                changes = []
            elif event.type == "player":
                self.player = event.data
                changes = []
            elif event.type == "say_result":
                self.on_say_result(event.data)
                changes = []
            elif event.type == "npc_line":
                changes = self._apply_npc_line(event.data)
            elif event.type == "dialogue_menu":
                self.in_dialogue = bool(event.data.get("open"))
                changes = []
            else:
                if event.type == "game_loaded":
                    self.status = Status()  # после загрузки сейва всё узнаём заново
                    self.last_place = None
                    self._left = None
                    self.in_dialogue = False
                changes = [self._describe_event(event)]
            # Реплики NPC — в своём списке (heard), иначе в городе вытеснят из «Недавно» всё остальное.
            self.recent.extend((self.updated, c.text) for c in changes if c.kind != "overheard")
            return changes

    def _apply_npc_line(self, data: dict) -> list[Change]:
        text = self.dialogue.text(str(data.get("file", "")), int(data.get("form", 0))) if self.dialogue else ""
        speaker = str(data.get("speaker") or "Кто-то")
        if not text:  # реплика мода, вздох, крик без текста
            return []
        if any(h.speaker == speaker and h.text == text and self.updated - h.at < HEARD_REPEAT for h in self.heard):
            return []
        heard = Heard(self.updated, speaker, text, bool(data.get("to_player")))
        self.heard.append(heard)
        return [Change("overheard", str(heard), subject=f"{speaker}: {text}")]

    def _apply_status(self, new: Status) -> list[Change]:
        old, changes = self.status, []
        if new.hold == "":
            new.hold = None  # в диких землях владение не всегда известно — помним прежнее
        if new.location is not None and new.location != old.location:
            if new.location:
                if not (self._left and self._left[1] == new.location):  # вернулись, не успев уйти, — молчим
                    changes.append(Change("location", f"новая локация: {new.location}" if old.location is not None
                                          else f"находимся: {new.location}", subject=new.location))
                self._left = None
            elif old.location:
                self.last_place = old.location
                self._left = (self.updated, old.location)  # засчитаем, если пробудем вне локации WILD_CONFIRM
            else:
                changes.append(Change("wilderness", "находимся: дикие земли", subject="дикие земли"))
        elif new.cell and new.cell != old.cell and new.interior and not old.interior:
            changes.append(Change("interior", f"вошли в помещение: {new.cell}", subject=new.cell))
        if self._left and new.location == "" and self.updated - self._left[0] >= WILD_CONFIRM:
            changes.append(Change("wilderness", f"ушли из локации {self._left[1]} в дикие земли",
                                  subject=f"дикие земли: {self._left[1]}"))
            self._left = None
        if (new.health is not None and new.health < LOW_HEALTH
                and (old.health is None or old.health >= LOW_HEALTH)):
            changes.append(Change("low_health", "игрок тяжело ранен"))
        # Новый снимок дополняет старый: незаполненные поля остаются прежними.
        self.status = old.model_copy(update=new.model_dump(exclude_none=True))
        if new.nearby is not None:
            self.status.nearby = new.nearby
        if new.gosling is not None:
            self.status.gosling = new.gosling
        return changes

    def _apply_markers(self, data: dict) -> list[Change]:
        markers = [Marker.model_validate(m) for m in data.get("markers", [])]
        world = data.get("worldspace")
        changes = []
        if world == self.map_world and self.markers:  # тот же мир — ищем новые места на карте
            was_visible = {m.name for m in self.markers if m.visible}
            changes = [Change("discovered", f"на карте появилось новое место: {m.name} ({m.kind})",
                              subject=f"карта: {m.name}")
                       for m in markers if m.visible and m.name not in was_visible]
        self.markers, self.map_world = markers, world
        return changes

    @staticmethod
    def _describe_event(event: GameEvent) -> Change:
        d = event.data
        match event.type:
            case "game_loaded":
                return Change("loaded", "игрок вернулся после перерыва")  # загрузка сейва; без слова «игра»
            case "combat_started":
                enemies = ", ".join(d.get("enemies", []))
                return Change("combat_started", f"начался бой: {enemies}" if enemies else "начался бой")
            case "combat_ended":
                return Change("combat_ended", "бой закончился")
            case "actor_killed":
                who = "игрок убил" if d.get("by_player") else "убит"
                return Change("killed", f"{who}: {d.get('name', '?')}")
            case "item_picked":
                count = d.get("count", 1)
                return Change("item", f"игрок подобрал: {d.get('name', '?')}" + (f" ×{count}" if count > 1 else ""))
        return Change("other", f"{event.type}: {d}" if d else event.type)

    def find_places(self, kind: str = "", name: str = "", max_km: float = 0, limit: int = 6) -> str:
        """Инструмент агента: места на карте игрока. В помещении — от точки, где вышли с улицы."""
        with self._lock:
            pos = self.status.pos
            if not self.markers or not pos:
                return "Карта пока недоступна (нет данных из игры)."
            found = find_places(pos[0], pos[1], self.markers, kind or "", name or "", (max_km or 0) * 1000,
                                self.map_cfg.unknown_radius, limit)
        if not found:
            return "Среди открытых игроком мест ничего подходящего. Неоткрытое видно только совсем рядом."
        return "\n".join(found)

    def gosling_near(self, max_distance: float) -> bool:
        """Гослинг-NPC загружен и рядом с игроком — можно говорить его голосом из игры."""
        g = self.status.gosling
        return bool(g and g.loaded and 0 <= g.distance <= max_distance)

    def player_details(self) -> str:
        """Инструмент агента: золото, что надето, что в сумке, активные эффекты."""
        with self._lock:
            p = dict(self.player)
        if not p:
            return "Сведений об игроке пока нет (нет данных из игры)."
        lines = []
        stats = [f"золото: {p.get('gold', 0)}", f"уровень {p.get('level', '?')}"]
        for key, label in (("health", "здоровье"), ("magicka", "магия"), ("stamina", "запас сил")):
            if isinstance(p.get(key), list) and len(p[key]) == 2:
                stats.append(f"{label} {round(p[key][0])}/{round(p[key][1])}")
        lines.append("Игрок: " + ", ".join(stats) + ".")
        if p.get("worn"):
            lines.append("Надето и в руках: " + ", ".join(p["worn"]) + ".")
        bag = []
        for category, info in (p.get("bag") or {}).items():
            top = ", ".join(f"{i['name']}" + (f" ×{i['count']}" if i["count"] > 1 else "") for i in info["top"])
            more = f" и ещё {info['kinds'] - len(info['top'])} видов" if info["kinds"] > len(info["top"]) else ""
            bag.append(f"{category} — {info['total']} шт.: {top}{more}")
        if bag:
            lines.append("В сумке: " + "; ".join(bag) + ".")
        effects = [e["name"] + (" (вредный)" if e.get("harmful") else "") for e in p.get("effects", [])]
        lines.append("Активные эффекты: " + (", ".join(effects) if effects else "нет") + ".")
        return "\n".join(lines)

    @property
    def in_combat(self) -> bool:
        return bool(self.status.in_combat)

    def describe(self) -> str:
        """Текст для {game_context} в промпте."""
        with self._lock:
            s = self.status
            if self.updated is None:
                return ""
            lines = []
            if s.location:
                where = s.location + (f" (владение {s.hold})" if s.hold and s.hold != s.location else "")
            elif s.location == "":
                where = "дикие земли" + (f", владение {s.hold}" if s.hold else "")
                if self.last_place:
                    where += f", по дороге от: {self.last_place}"
            else:
                where = ""
            if s.cell and s.cell != s.location:
                where = f"{where}, {s.cell}" if where else s.cell
            if where:
                if s.interior is not None:
                    where += ", в помещении" if s.interior else ", под открытым небом"
                lines.append(f"Место: {where}.")
            if s.interior is False and s.pos and self.markers:
                c = self.map_cfg
                lines.extend(describe_surroundings(s.pos[0], s.pos[1], self.markers, s.location,
                                                   c.known_count, c.unknown_radius, c.unknown_count))
            when = []
            if s.hour is not None:
                when.append(time_of_day(s.hour))
            if s.weather:
                when.append(f"погода: {s.weather}")
            if when:
                lines.append("Время: " + ", ".join(when) + ".")
            player = []
            if s.health is not None:
                player.append(f"здоровье {round(s.health * 100)}%")
            if s.level is not None:
                player.append(f"уровень {s.level}")
            if s.sneaking:
                player.append("крадётся")
            if player:
                lines.append("Игрок: " + ", ".join(player) + ".")
            if s.in_combat is not None:
                lines.append("Сейчас идёт бой!" if s.in_combat else "Боя нет.")
            if s.nearby:
                followers = [n.name for n in s.nearby if n.follower]
                if len(followers) > 1:
                    lines.append("Спутники игрока (одного из них игра показывает как тебя): "
                                 + ", ".join(followers) + ".")
                alive = [n for n in s.nearby if not n.dead and not n.follower]
                enemies = [n.name for n in alive if n.hostile]
                others = [n.name for n in alive if not n.hostile]
                if enemies:
                    lines.append("Враги рядом: " + ", ".join(enemies) + ".")
                if others:
                    lines.append("Рядом: " + ", ".join(others) + ".")
            recent = self._recent()
            if recent:
                lines.append("Недавно: " + "; ".join(recent) + ".")
            heard = [str(h) for h in self.heard if time.time() - h.at < HEARD_SECONDS]
            if heard:
                lines.append("Ты слышал рядом (последние реплики NPC): " + " ".join(heard[-6:]))
            return "\n".join(lines)

    def _recent(self) -> list[str]:
        """События за RECENT_SECONDS; одинаковые подряд схлопываются: «игрок убил: Волк ×3»."""
        since = time.time() - RECENT_SECONDS
        grouped: list[list] = []
        for t, text in self.recent:
            if t < since:
                continue
            if grouped and grouped[-1][0] == text:
                grouped[-1][1] += 1
            else:
                grouped.append([text, 1])
        return [text if n == 1 else f"{text} ×{n}" for text, n in grouped]

    def snapshot(self) -> dict:
        with self._lock:
            return {"status": self.status.model_dump(exclude_none=True),
                    "recent": self._recent(), "updated": self.updated}
