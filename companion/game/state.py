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
    nearby     — [{"name": "Назим", "hostile": false, "dead": false, "follower": false, "unique": true}, ...]
                 follower — спутник игрока; это тело Гослинга в игре, в «Рядом» его не показываем;
                 unique — единственный в игре (именной NPC, а не «Драугр»)
    threats    — живые враги дальше «рядом» (до ~140 м: дракон в небе) — те же поля, что у nearby
                 (и в nearby: race — раса, если не людская: «Дракон»; distance — м)
    partner    — {"name", "ago" (с), "distance" (м), "hostile", "dead", "unique", "gone"}: с кем игрок заговорил
                 (5 минут после разговора); нет поля в полном status — собеседника нет

Разовые события:
    game_loaded     — {}  (загрузка сохранения)
    combat_started  — {"enemies": ["Драугр-воитель", ...], "races": {"Мирмулнир": "Дракон"}}
    combat_ended    — {}
    actor_killed    — {"name": "Волк", "by_player": true, "unique": false}
    item_picked     — {"name": "Двемерская шестерня", "count": 1}
    activated       — {"name", "kind", "unique"}: игрок задействовал что-то; kind — activator (рычаг, цепь, столб),
                      container, body (обыскал тело), book, furniture, npc (заговорил — это и есть собеседник)
    unlocked        — {"name", "door"}: замок рядом с игроком открылся без взлома — ключом
    npc_flees       — {"name"}: собеседник бежит прочь от игрока
    shout           — {"name"}: игрок применил Крик
    book_read       — {"name", "file", "form"}: Владос открыл книгу (из сумки или с полки) — Гослинг знает её
                      («читал Владос», companion/knowledge.py); повод мозгу reading
    gosling_items   — {"given": [{"name", "count", "book", "file", "form"}], "taken": [...]}: Владос отдал Гослингу
                      или забрал у него; отданную книгу Гослинг читает сам; повод мозгу gift (важный)
    player_died     — {"killer", "race", "unique"}: Владос погиб (killer "" — упал, утонул, ловушка). Гослинг
                      говорит об этом после загрузки сейва: событие died вместо loaded

Встречи (раса врага, Крик) — через meet (память, companion/memory/keeper.py): впервые — событие novel
(срочное), потом — пометка «уже встречали N раз, последний — там-то».
    map_markers     — {"worldspace": "Скайрим", "markers": [{"name", "type", "x", "y", "visible", "travel"}]}
                      все маркеры карты; шлётся при загрузке, смене мира и открытии нового места
    quests          — {"quests": [{"id", "name", "type", "stage", "objectives", "aliases", "log", "tracked"}],
                       "completed": [id, ...]}
                      активные квесты журнала (companion/game/quests.py); при загрузке и смене этапа/цели.
                      Сравнение с прошлым журналом -> события «новое задание», «цель выполнена», «новая цель»
    player          — {"gold", "level", "health", "magicka", "stamina", "worn", "bag", "effects"}
                      подробности об игроке (инструмент player_details); при смене инвентаря/экипировки
    say_result      — {"id", "ok", "reason"}: Гослинг произнёс реплику (или почему нет) — для голоса из NPC
    npc_line        — {"speaker", "file", "form", "category", "subtype", "to_player"}: NPC рядом (~25 м) начал
                      реплику; текст — по файлу плагина и номеру INFO (companion/game/dialogue.py).
                      to_player — игрок в это время в меню разговора (скорее всего, говорят с ним)
    dialogue_menu   — {"open"}: игрок открыл/закрыл меню разговора с NPC
    profile         — {"race", "female", "level", "skills", "factions", "crime", "quests_done"}: профиль игрока
                      (companion/game/profile.py); при загрузке, смене этапа квеста и раз в минуту
    stat            — {"name", "value"}: игровая статистика («Locks Picked», …), накопленное значение;
                      рост счётчика из MOMENTS — событие («выучил слово силы», «что-то украл»)

«Недавно» в {game_context}: события с давностью; бой — одной строкой; важное (Change.important) держится дольше.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from companion.config import MapConfig
from companion.game.books import BookLibrary
from companion.game.dialogue import DialogueTexts
from companion.game.profile import PlayerProfile
from companion.game.geo import Marker, describe_surroundings, find_places
from companion.game.quests import QuestLog

log = logging.getLogger(__name__)

LOW_HEALTH = 0.3
RECENT_SECONDS = 600  # «недавно» — события за последние 10 минут
WILD_CONFIRM = 8      # секунд вне локации, чтобы засчитать уход: на границе игра дёргает локацию туда-сюда
HEARD_SECONDS = 300   # реплики NPC рядом помним 5 минут (для ответа на «что он сказал?»)
HEARD_REPEAT = 10     # та же реплика того же NPC за 10 с — повтор события, не новая


IMPORTANT_SECONDS = 1800  # важное (слово силы, задание, убит знакомый) держится в «Недавно» полчаса
RECENT_LINES = 10      # строк в «Недавно» — не больше (сначала уходит старое неважное)
FIGHT_MERGE = 90       # бой начался через ≤ 90 с после прошлого — это одна стычка (зал склепа, засада)
FIGHT_TAIL = 10        # смерть врага приходит и после «бой закончился» — ещё относится к бою
JUST_NOW = 45          # секунд — «только что»
QUEST_CONTEXT = 3      # заданий с текущими целями — в «Сейчас в игре»
QUEST_FRESH = 1800     # задание, менявшееся за полчаса, — первым в «Сейчас в игре»
DEATH_BEFORE = 300     # секунд «Недавно» перед гибелью — что пошло не так (бой, ранение)
DEATH_LINES = 4        # строк из них — не больше
BOOK_EXCERPT = 600     # знаков начала отданной книги — мозгу, чтобы было о чём сказать
READING_EXCERPT = 250  # знаков начала книги, которую читает Владос, — в повод (без него модель гадала о содержании)

# Игровая статистика -> событие для «Недавно» и мозга: (вид, текст, важное). Событие статистики игра шлёт,
# когда счётчик растёт, — то есть ровно в момент поступка.
MOMENTS: dict[str, tuple[str, str, bool]] = {
    "Words Of Power Learned": ("power", "игрок выучил новое слово силы со стены", True),
    "Words Of Power Unlocked": ("power", "игрок открыл слово силы — потратил душу дракона", True),
    "Dragon Souls Collected": ("power", "игрок поглотил душу дракона", True),
    "Shouts Learned": ("power", "игрок выучил новый крик (Ту'ум)", True),
    "Shouts Mastered": ("power", "игрок полностью освоил крик", True),
    "Werewolf Transformations": ("power", "игрок обратился в оборотня", True),
    "Dungeons Cleared": ("cleared", "подземелье зачищено", True),
    "Murders": ("crime", "игрок совершил убийство — по закону это преступление", True),
    "Times Jailed": ("crime", "игрок попал в тюрьму", True),
    "Jail Escapes": ("crime", "игрок сбежал из тюрьмы", True),
    "Horses Stolen": ("crime", "игрок угнал лошадь", True),
    "Necks Bitten": ("crime", "игрок укусил кого-то, как вампир", True),
    "Items Stolen": ("crime", "игрок что-то украл", False),
    "Pockets Picked": ("crime", "игрок обчистил чей-то карман", False),
    "Assaults": ("crime", "игрок напал на кого-то — по закону это преступление", False),
    "Persuasions": ("talk", "игрок уговорил собеседника", False),
    "Bribes": ("talk", "игрок подкупил собеседника", False),
    "Intimidations": ("talk", "игрок запугал собеседника", False),
    "Locks Picked": ("deed", "игрок взломал замок", False),
    "Spells Learned": ("deed", "игрок выучил заклинание", False),
}


# Поступок, из которого ясно, кого встретили (душа дракона — значит, был дракон): [(категория, ключ)] для meet.
# Раса драконов в русской игре — «dragon race» (не переведена), в памяти встреч — «dragon».
MOMENT_MEETS = {"Dragon Souls Collected": [("раса", "dragon"), ("раса", "дракон")]}


@dataclass(frozen=True)
class Change:
    """Что изменилось в игре. kind — для мозга, text — для промпта и журнала."""
    kind: str  # loaded, died (загрузка после гибели — вместо loaded), player_died (погиб, ещё до загрузки),
    #            location, interior, low_health, combat_started, combat_ended, killed, item, overheard,
    #            profile (вступил в гильдию, завершил квест, розыск...), quest, quest_misc (задания журнала),
    #            person (знакомый NPC напал или убит), power (слово силы, душа дракона), cleared, crime, talk,
    #            deed (статистика игры), other
    text: str
    subject: str = ""  # о чём событие (место) — чтобы не комментировать одно и то же дважды
    important: bool = False  # дольше держится в «Недавно», не тонет среди убитых злокрысов


class GameEvent(BaseModel):
    type: str
    data: dict[str, Any] = {}


class Nearby(BaseModel):
    name: str
    hostile: bool = False
    dead: bool = False
    follower: bool = False
    unique: bool | None = None  # единственный в игре (Арвел, Белетор), а не «Драугр»; None — плагин не прислал
    race: str = ""              # раса, если не людская («Дракон», «Драугр»)
    distance: float | None = None  # метров

    @property
    def title(self) -> str:
        """«Мирмулнир (дракон)»; «Драугр» — без повтора."""
        race = race_name(self.race).lower()
        return f"{self.name} ({race})" if race and race not in self.name.lower() else self.name


@dataclass(frozen=True)
class Heard:
    """Реплика NPC, которую слышал Гослинг."""
    at: float  # time.time()
    speaker: str
    text: str
    to_player: bool  # говорили с игроком (он был в меню разговора)

    def __str__(self) -> str:
        return f"{self.speaker}{' (игроку)' if self.to_player else ''}: «{self.text}»"

    @classmethod
    def parse(cls, line: str, at: float) -> Heard | None:
        """Обратно из str(Heard) — так реплика записана в журнале памяти."""
        if not (m := re.fullmatch(r"(.+?)( \(игроку\))?: «(.*)»", line, re.S)):
            return None
        return cls(at, m[1], m[3], bool(m[2]))


class GoslingStatus(BaseModel):
    loaded: bool = False
    distance: float = -1  # метров до игрока; -1 — не загружен
    follower: bool = False
    health: float | None = None  # своё здоровье 0..1 (только когда загружен)
    bleedout: bool = False       # упал без сил — спутника почти добили
    dead: bool = False


class Partner(BaseModel):
    """С кем игрок недавно заговорил (плагин следит 5 минут)."""
    name: str
    ago: float = 0          # секунд с начала разговора
    distance: float | None = None  # метров
    hostile: bool = False
    dead: bool = False
    unique: bool | None = None
    gone: bool = False      # не видно: выгружен или остался в другом помещении


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
    threats: list[Nearby] | None = None
    partner: Partner | None = None


# Что игрок активировал (событие activated): вид -> (вид события, начало текста)
ACTIVATED = {
    "activator": ("activated", "игрок задействовал"),   # рычаг, цепь, столб загадки
    "container": ("looted", "игрок открыл"),
    "body": ("looted", "игрок обыскал тело"),
    "book": ("reading", "игрок читает"),
    "furniture": ("furniture", "игрок воспользовался"),
    "npc": ("talked", "игрок заговорил с"),
}


def race_name(race: str) -> str:
    """Название расы для текста и памяти встреч: часть рас в русской игре не переведена («dragon race») —
    хвост « race» убираем."""
    race = race.strip()
    return race[:-5] if race.lower().endswith(" race") else race


def ago(seconds: float) -> str:
    if seconds < JUST_NOW:
        return "только что"
    minutes = round(seconds / 60)
    return f"{max(1, minutes)} мин назад" if minutes < 60 else f"{round(minutes / 60)} ч назад"


def time_of_day(hour: float) -> str:
    h = int(hour) % 24
    part = ("ночь" if h < 5 else "утро" if h < 12 else "день" if h < 17 else "вечер" if h < 22 else "ночь")
    return f"{part}, {h:02d}:{int(hour % 1 * 60):02d}"


class GameState:
    """Последнее известное состояние игры + недавние события. Потокобезопасно."""

    def __init__(self, map_cfg: MapConfig | None = None, interface_bsa: Path | None = None, recent: int = 100,
                 dialogue: DialogueTexts | None = None, profile: PlayerProfile | None = None,
                 books: BookLibrary | None = None):
        self._lock = threading.Lock()
        self.map_cfg = map_cfg or MapConfig()
        self.quest_log = QuestLog(interface_bsa or Path("Skyrim - Interface.bsa"))
        self.markers: list[Marker] = []
        self.map_world: str | None = None
        self.status = Status()
        self.recent: deque[tuple[float, Change]] = deque(maxlen=recent)  # (time.time(), что случилось)
        self.updated: float | None = None  # time.time() последнего события
        self.last_place: str | None = None  # откуда ушли в дикие земли
        self._left: tuple[float, str] | None = None  # (когда, откуда) — уход ещё не подтверждён
        self.player: dict = {}  # подробности об игроке (событие player)
        self.on_say_result: Callable[[dict], None] = lambda data: None  # голос из NPC ждёт подтверждения
        self.dialogue = dialogue  # тексты реплик игры; None — реплики NPC не слышим
        self.books = books  # тексты книг игры: отданную книгу Гослинг читает — мозгу её начало
        self.heard: deque[Heard] = deque(maxlen=12)
        self.in_dialogue = False  # игрок в меню разговора с NPC
        self.profile = profile or PlayerProfile()  # кто игрок по данным игры: гильдии, розыск, квесты, привычки
        self.player_name = "Владос"  # имя персонажа в игре (из status / game_loaded)
        # Встреча (категория, ключ) -> (сколько раз встречали раньше, где и когда последний раз) | None (не считаем).
        # Ставит память (MemoryKeeper.meet); без неё встреч не помним.
        self.meet: Callable[[str, str], tuple[int, str] | None] = lambda category, key: None
        # Гибель Владоса: до загрузки он мёртв (Гослинг молчит), после — повод died. Живёт вне «Недавно» и вне
        # памяти по сейву: загрузка сейва постарше стёрла бы её. Сколько раз погибал от кого — за этот запуск.
        self.dead = False
        self._death: str | None = None
        self._deaths: Counter[str] = Counter()

    def apply(self, event: GameEvent) -> list[Change]:
        """Учитывает событие; возвращает, что изменилось."""
        with self._lock:
            self.updated = time.time()
            if event.data.get("player_name"):
                self.player_name = str(event.data["player_name"])
            if event.type == "status":
                changes = self._apply_status(Status.model_validate(event.data))
            elif event.type == "map_markers":
                changes = self._apply_markers(event.data)
            elif event.type == "quests":
                changes = [Change(kind, text, subject=subject, important=important)
                           for kind, text, subject, important in self.quest_log.update(event.data)]
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
            elif event.type == "profile":
                # «Завершил квест «…»» уже пришло от журнала заданий («задание … завершено») — не дублируем.
                closed = {c.subject for _t, c in self.recent if c.kind in ("quest", "quest_misc")}
                changes = [Change("profile", text, subject=text, important=True)
                           for text in self.profile.update(event.data, self.player_name)
                           if not ((m := re.search(r"завершил квест «(.+)»", text)) and f"задание: {m[1]}" in closed)]
            elif event.type == "stat":
                changes = self._apply_stat(str(event.data.get("name", "")), int(event.data.get("value", 0)))
            elif event.type == "player_died":
                changes = self._apply_death(event.data)
            else:
                if event.type == "game_loaded":
                    self.profile.reset()  # следующий профиль — точка отсчёта (сейв мог быть постарше)
                    self.quest_log.reset()  # и журнал заданий тоже
                    self.status = Status()  # после загрузки сейва всё узнаём заново (и собеседника, и угрозы)
                    # «Недавно» и подслушанное — из другого прошлого: умер в бою с драконом, загрузил сейв до боя —
                    # «погибли: Мирмулнир» остаться не должно. Подслушанное до сейва вернёт память (MemoryKeeper).
                    self.recent.clear()
                    self.heard.clear()
                    self.dead = False
                    self.last_place = None
                    self._left = None
                    self.in_dialogue = False
                changes = self._describe_event(event)
            # Реплики NPC — в своём списке (heard), иначе в городе вытеснят из «Недавно» всё остальное; записи журнала
            # заданий — только в память (длинные; цели и так в «Сейчас в игре»).
            self.recent.extend((self.updated, c) for c in changes if c.kind not in ("overheard", "quest_log"))
            return changes

    def _apply_stat(self, name: str, value: int) -> list[Change]:
        """Статистика игры: в привычки — всегда, а поступок (слово силы, кража) — ещё и событием."""
        old = self.profile.stats.get(name)
        self.profile.stat(name, value)
        log.info("Статистика игры: %s = %d", name, value)
        moment = MOMENTS.get(name)
        if not moment or (old is not None and value <= old):
            return []
        kind, text, important = moment
        for meet in MOMENT_MEETS.get(name, []):
            self.meet(*meet)
        return [Change(kind, text, subject=f"{text} {value}", important=important)]

    def _apply_death(self, d: dict) -> list[Change]:
        """Владос погиб. Сейчас — только в «Недавно» (Гослинг молчит, пока он мёртв); что пошло не так —
        запоминаем до загрузки: там «Недавно» очистится, а Гослинг скажет об этом прямо (событие died)."""
        killer = Nearby(name=str(d["killer"]), race=str(d.get("race") or "")).title if d.get("killer") else ""
        s = self.status
        place = ", ".join(dict.fromkeys(x for x in (s.location, s.cell) if x))
        before = [r for r in self._recent(since=self.updated - DEATH_BEFORE)
                  if not r.startswith("Владос погиб")][-DEATH_LINES:]  # прошлая гибель — не «перед этим»
        self._deaths[killer] += 1
        times = self._deaths[killer]
        how = (f"его убил {killer}" if killer else "без убийцы — упал, утонул или ловушка") + (
            f", {place}" if place else "") + (
            f"; {'от него' if killer else 'так'} гибнет уже {times}-й раз за вечер" if times > 1
            else "; за вечер — впервые")
        self.dead = True
        self._death = (f"Владос погиб ({how})" + (f". Перед этим: {'; '.join(before)}" if before else "")
                       + ". Он уже вернулся к последнему сохранению — вы снова там, где были тогда, а что было после, "
                         "ещё не случилось. Ты это помнишь и знаешь, что он погиб: скажи прямо, с шуткой, что пошло "
                         "не так и что в этот раз сделать иначе")
        log.info("Игрок погиб: %s", how)
        return [Change("player_died", f"Владос погиб ({how})", subject="Владос погиб", important=True)]

    def _known(self, name: str, unique: bool | None = None) -> bool:
        """Знакомый человек, а не «Драугр»: собеседник игрока, единственный в игре, говорил рядом,
        или он из задания журнала."""
        if self.status.partner and self.status.partner.name == name:
            return True
        if unique is not None:
            return unique
        return (any(h.speaker == name for h in self.heard)
                or any(name in q.aliases.values() for q in self.quest_log.quests))

    def _apply_npc_line(self, data: dict) -> list[Change]:
        file, form = str(data.get("file", "")), int(data.get("form", 0))
        text = self.dialogue.text(file, form) if self.dialogue else ""
        speaker = str(data.get("speaker") or "Кто-то")
        if not text:  # реплика мода, вздох, крик без текста
            return []
        if any(h.speaker == speaker and h.text == text and self.updated - h.at < HEARD_REPEAT for h in self.heard):
            return []
        changes = []
        # Ответ на выбор игрока в меню разговора — сначала то, что выбрал он («Кого вы ищете?»): сам игрок
        # молчит, текст выбора — в файлах игры (companion/game/dialogue.py).
        if data.get("to_player") and self.dialogue and (said := self.dialogue.prompt(file, form)):
            mine = Heard(self.updated, self.player_name, said, False)
            self.heard.append(mine)
            changes.append(Change("overheard", f"{mine} (выбрал в разговоре с {speaker})", subject=f"игрок: {said}"))
        heard = Heard(self.updated, speaker, text, bool(data.get("to_player")))
        self.heard.append(heard)
        changes.append(Change("overheard", str(heard), subject=f"{speaker}: {text}"))
        return changes

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
        # Своё здоровье Гослинга: тяжело ранен, упал без сил — срочные поводы, как ранение игрока.
        og, ng = old.gosling, new.gosling
        if ng and ng.loaded and not ng.dead:
            if ng.bleedout and not (og and og.bleedout):
                changes.append(Change("hurt_self", "ты, Гослинг, упал без сил — тебя самого почти добили, драться ты не можешь",
                                      important=True))
            elif (ng.health is not None and ng.health < LOW_HEALTH and not ng.bleedout
                  and (og is None or og.health is None or og.health >= LOW_HEALTH)):
                changes.append(Change("hurt_self", f"ты, Гослинг, сам тяжело ранен — твоё здоровье {round(ng.health * 100)}%",
                                      important=True))
        # Знакомый был мирным — и стал врагом (Арвел вырвался из паутины и напал). Из «рядом» и из собеседника.
        was_peaceful = {n.name for n in old.nearby or [] if not n.hostile and not n.dead}
        if old.partner and not old.partner.hostile and not old.partner.dead and not old.partner.gone:
            was_peaceful.add(old.partner.name)
        now_hostile = {n.name: n.unique for n in new.nearby or [] if n.hostile and not n.dead}
        if new.partner and new.partner.hostile and not new.partner.dead:
            now_hostile.setdefault(new.partner.name, new.partner.unique)
        for name, unique in now_hostile.items():
            if name in was_peaceful and self._known(name, unique):
                changes.append(Change("person", f"{name} напал на вас — теперь враг",
                                      subject=f"враг: {name}", important=True))
        if new.threats is not None:
            # Враг вдали, которого не было (дракон показался в небе) — если это не люди: людей вдали слишком много.
            seen = {n.name for n in (old.threats or []) + (old.nearby or [])}
            for n in new.threats:
                if n.race and n.name not in seen:
                    changes.extend(self._met_changes(
                        "threat", f"вдали враг: {n.title}" + (f", ~{round(n.distance)} м" if n.distance else ""),
                        [n.race]))
        # Новый снимок дополняет старый: незаполненные поля остаются прежними.
        self.status = old.model_copy(update=new.model_dump(exclude_none=True))
        if new.nearby is not None:
            self.status.nearby = new.nearby
        if new.gosling is not None:
            self.status.gosling = new.gosling
        if new.nearby is not None:  # status пришёл целиком: нет собеседника — значит, разговор давно был
            self.status.partner = new.partner
            self.status.threats = new.threats
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

    def _met_changes(self, kind: str, text: str, races: list[str], subject: str = "") -> list[Change]:
        """Событие о встрече с существами races: кого видим впервые — отдельное срочное событие novel,
        кого уже встречали — пометка в тексте («уже встречали 2 раза, последний — Западная сторожевая башня»)."""
        changes, notes = [], []
        for race in dict.fromkeys(race_name(r).lower() for r in races if r):
            seen = self.meet("раса", race)
            if seen is None:
                continue
            count, last = seen
            if count == 0:
                changes.append(Change("novel", f"{race}: таких ты видишь впервые в жизни",
                                      subject=f"впервые: {race}", important=True))
            else:
                notes.append(f"{race} — уже встречали {count} раз" + (f", последний — {last}" if last else ""))
        main = Change(kind, text + (f" ({'; '.join(notes)})" if notes else ""), subject=subject or text,
                      important=bool(notes) and kind == "threat")
        return [main, *changes]

    def _describe_event(self, event: GameEvent) -> list[Change]:
        d = event.data
        match event.type:
            case "game_loaded":
                if death := self._death:  # загрузился после гибели — это не «перерыв»
                    self._death = None
                    return [Change("died", death, subject="гибель Владоса", important=True)]
                return [Change("loaded", "игрок вернулся после перерыва")]  # загрузка сейва; без слова «игра»
            case "combat_started":
                races = d.get("races") or {}
                enemies = ", ".join(Nearby(name=n, race=races.get(n, "")).title for n in d.get("enemies", []))
                # Кого видим впервые — отдельным событием после самого боя (сводка боя начинается с combat_started).
                return self._met_changes("combat_started", f"начался бой: {enemies}" if enemies else "начался бой",
                                         list(races.values()))
            case "combat_ended":
                return [Change("combat_ended", "бой закончился")]
            case "actor_killed":
                name = Nearby(name=str(d.get("name") or "?"), race=str(d.get("race") or "")).title
                who = "игрок убил" if d.get("by_player") else "убит"
                if self._known(name, d.get("unique")):  # не злокрыс из толпы — отдельной заметной строкой
                    p = self.status.partner
                    talked = (" (игрок с ним недавно говорил)" if p and p.name == name
                              else " (вы его слышали недавно)" if any(h.speaker == name for h in self.heard) else "")
                    return [Change("person", f"{who}: {name}{talked}", subject=f"убит: {name}", important=True)]
                return [Change("killed", f"{who}: {name}", subject=name)]
            case "activated":
                kind, verb = ACTIVATED.get(str(d.get("kind")), ("activated", "игрок задействовал"))
                name = str(d.get("name") or "?")
                return [Change(kind, f"{verb}: {name}", subject=f"{verb}: {name}",
                               important=kind == "looted" and bool(d.get("unique")))]  # обыскал тело Арвела
            case "unlocked":
                name = str(d.get("name") or "?")
                return [Change("unlocked", f"игрок отпер ключом: {name}", subject=f"отпер: {name}")]
            case "npc_flees":
                name = str(d.get("name") or "?")
                return [Change("person", f"{name} убегает от вас", subject=f"убегает: {name}", important=True)]
            case "shout":
                name = str(d.get("name") or "?")
                seen = self.meet("крик", name)
                note = ("" if seen is None else " — впервые на твоих глазах" if seen[0] == 0
                        else f" — этот Крик ты видел уже {seen[0]} раз")
                return [Change("power", f"игрок применил Крик: {name}{note}", subject=f"крик: {name}",
                               important=True)]
            case "book_read":
                name = str(d.get("name") or "?")
                # «Через плечо» модель поняла как «краем глаза, не помню» и справку не звала (2026-10-16): говорим
                # прямо, где содержание.
                # И начало текста: на сам повод модель отвечает без справок и гадала о содержании («Ловушка Судьбы —
                # роман про шпионов»).
                book = self.books.get(str(d.get("file", "")), int(d.get("form", 0))) if self.books else None
                start = f"; начало: «{book.text[:READING_EXCERPT].rsplit(' ', 1)[0]}…»" if book else ""
                return [Change("reading", f"Владос читает книгу «{name}» — ты читал вместе с ним; что в ней, "
                                          f"вспомнишь справкой world_info («{name}»){start}", subject=f"книга: {name}")]
            case "gosling_items":
                return self._exchange(d)
            case "item_picked":
                count = d.get("count", 1)
                return [Change("item", f"игрок подобрал: {d.get('name', '?')}" + (f" ×{count}" if count > 1 else ""))]
        return [Change("other", f"{event.type}: {d}" if d else event.type)]

    def _exchange(self, d: dict) -> list[Change]:
        """Владос отдал Гослингу вещи или забрал. Отданную книгу Гослинг прочитал — мозгу её начало, чтобы было
        о чём сказать (о прочитанном спросят — справка world_info)."""
        def items(rows: list) -> str:
            return ", ".join(f"{r.get('name')}" + (f" ×{r['count']}" if r.get("count", 1) > 1 else "") for r in rows)

        given = [r for r in d.get("given") or [] if r.get("name")]
        taken = [r for r in d.get("taken") or [] if r.get("name")]
        parts = []
        if given:
            parts.append(f"Владос отдал тебе — теперь это у тебя в сумке: {items(given)}")
        if taken:
            parts.append(f"Владос забрал у тебя — теперь это у него: {items(taken)}")
        if not parts:
            return []
        text = "; ".join(parts)
        book = next((self.books.get(str(r.get("file", "")), int(r.get("form", 0)))
                     for r in given if r.get("book") and self.books), None)
        if book:
            start = book.text[:BOOK_EXCERPT].rsplit(" ", 1)[0]
            text += (f". Книгу «{book.title}» ты сразу прочитал — вот её начало: «{start}…». Можешь сказать, "
                     "что думаешь о ней")
        return [Change("gift", text, subject=text[:80], important=True)]

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
            if (g := s.gosling) and g.loaded and g.health is not None:  # своё — чтобы знал, что ранен и он
                lines.append("Ты сам (Гослинг): " + ("упал без сил — тебя почти добили" if g.bleedout
                                           else f"здоровье {round(g.health * 100)}%") + ".")
            if s.in_combat is not None:
                lines.append("Сейчас идёт бой!" if s.in_combat else "Боя нет.")
            if s.nearby:
                followers = [n.name for n in s.nearby if n.follower]
                if len(followers) > 1:
                    lines.append("Спутники игрока (одного из них игра показывает как тебя): "
                                 + ", ".join(followers) + ".")
                alive = [n for n in s.nearby if not n.dead and not n.follower]
                enemies = [n.title for n in alive if n.hostile]
                others = [n.title for n in alive if not n.hostile]
                if enemies:
                    lines.append("Враги рядом: " + ", ".join(enemies) + ".")
                if others:
                    lines.append("Рядом: " + ", ".join(others) + ".")
            if s.threats:
                lines.append("Враги вдали: " + ", ".join(
                    n.title + (f" — ~{round(n.distance)} м" if n.distance else "") for n in s.threats) + ".")
            if p := s.partner:
                state = ("мёртв" if p.dead else "не видно — ушёл или остался позади" if p.gone
                         else ("враг, " if p.hostile else "") + f"~{round(p.distance or 0)} м от игрока")
                lines.append(f"Собеседник (игрок заговорил с ним {ago(p.ago)}): {p.name} — {state}.")
            if self.player:  # коротко, чтобы и сам мог предложить продать хлам; подробно — player_details
                bag = ", ".join(f"{category} — {info.get('total', 0)} шт."
                                for category, info in (self.player.get("bag") or {}).items())
                lines.append(f"У Владоса: золото {self.player.get('gold', 0)}" + (f"; в сумке: {bag}" if bag else "")
                             + " (что именно — player_details).")
            if quests := self.quest_log.context(QUEST_CONTEXT, QUEST_FRESH):
                lines.append(quests)
            recent = self._recent()
            if recent:
                lines.append("Недавно (сверху — раньше, внизу — последнее):\n" + "\n".join(f"- {r}" for r in recent))
            heard = [str(h) for h in self.heard if time.time() - h.at < HEARD_SECONDS]
            if heard:
                lines.append("Ты слышал рядом (реплики NPC и что Владос выбирал в разговоре с ними): " + " ".join(heard[-8:]))
            return "\n".join(lines)

    def _recent(self, now: float | None = None, since: float | None = None) -> list[str]:
        """«Недавно»: события с давностью, старые сверху.
        - бой (и стычки одна за другой) — одной строкой: с кем и кого убили, а не «убил: Злокрыс» по строке;
        - важное (слово силы, задание, знакомый напал или убит) держится IMPORTANT_SECONDS и не вытесняется;
        - одинаковые подряд схлопываются: «игрок взломал замок ×3».
        since — только события после этого момента и без давности (для истории разговора)."""
        now = now or time.time()
        items: list[list] = []  # [когда, важное, текст, сколько раз]
        fight: dict | None = None

        def flush() -> None:
            nonlocal fight
            if fight:
                items.append([fight["start"], False, self._fight_text(fight, now), 1])
                fight = None

        for t, c in self.recent:
            too_old = (t <= since) if since is not None else (
                now - t > (IMPORTANT_SECONDS if c.important else RECENT_SECONDS))
            if too_old:
                continue
            if c.kind == "combat_started":
                enemies = c.text.partition(": ")[2]
                if fight and (fight["end"] is None or t - fight["end"] <= FIGHT_MERGE):
                    fight["end"] = None  # новая стычка сразу после прошлой — тот же бой
                else:
                    flush()
                    fight = {"start": t, "end": None, "enemies": Counter(), "kills": Counter(), "others": Counter()}
                fight["enemies"].update({e: 0 for e in enemies.split(", ") if e})  # кто был — по именам
            elif c.kind == "combat_ended":
                if fight:
                    fight["end"] = t
            elif c.kind == "killed" and fight and (fight["end"] is None or t - fight["end"] <= FIGHT_TAIL):
                fight["kills" if c.text.startswith("игрок убил") else "others"][c.subject] += 1
            else:
                if fight and fight["end"] is not None:
                    flush()  # бой позади, дальше другое — строка боя встаёт на своё место по времени
                if items and items[-1][2] == c.text:
                    items[-1][0], items[-1][3] = t, items[-1][3] + 1
                else:
                    items.append([t, c.important, c.text, 1])
        flush()
        items.sort(key=lambda i: i[0])
        # Не больше RECENT_LINES строк: лишнее — старое неважное.
        while len(items) > RECENT_LINES and (ordinary := [i for i in items if not i[1]]):
            items.remove(ordinary[0])
        return [("" if since is not None else f"{ago(now - t)}: ") + text + (f" ×{n}" if n > 1 else "")
                for t, _imp, text, n in items]

    @staticmethod
    def _fight_text(fight: dict, now: float) -> str:
        def names(counter: Counter) -> str:
            return ", ".join(name if n == 1 else f"{name} ×{n}" for name, n in counter.items())

        ongoing = fight["end"] is None
        minutes = ((now if ongoing else fight["end"]) - fight["start"]) / 60
        text = ("идёт бой" if ongoing else "был бой") + (f" ({round(minutes)} мин)" if minutes >= 1.5 else "")
        if fight["enemies"]:
            text += " с: " + ", ".join(fight["enemies"])
        if fight["kills"]:
            text += " — игрок убил: " + names(fight["kills"])
        if fight["others"]:
            text += "; погибли: " + names(fight["others"])
        return text

    def events_since(self, since: float) -> list[str]:
        """Что случилось после момента since (сжато, как «Недавно», но без давности) — для истории разговора:
        что произошло между репликами."""
        with self._lock:
            return self._recent(since=since)

    def snapshot(self) -> dict:
        with self._lock:
            return {"status": self.status.model_dump(exclude_none=True),
                    "recent": self._recent(), "updated": self.updated}
