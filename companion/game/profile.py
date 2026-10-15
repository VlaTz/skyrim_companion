"""Профиль Владоса по данным игры: кто он, гильдии и ранги, розыск, навыки, пройденные квесты, привычки.

Плагин присылает profile (при загрузке, смене этапа квеста, раз в минуту) и stat (игровая статистика:
«Locks Picked», «Items Stolen» … — накопленное значение за всю игру). Здесь:
- фракции (файл + номер) -> гильдии с русскими рангами (названия рангов в игре не переведены), фракции
  преступлений -> владения розыска (их названия — в Skyrim.esm);
- сравнение с прошлым профилем -> события: вступил, новый ранг, вышел, завершил квест, награда за голову,
  новый уровень (первый профиль после загрузки сейва — точка отсчёта, без событий);
- статистика -> привычки по делам («взломано замков 34»);
- summary() — «Владос по игре» для {memory}.

Таны: отдельной фракции в игре нет (титул — в переменных квестов), пока не показываем.
"""

from __future__ import annotations

import logging
import struct
import threading
from pathlib import Path

from companion.game.gamefiles import load_strings
from companion.game.places import PLUGINS, _edid, _read_plugin

log = logging.getLogger(__name__)

Key = tuple[str, int]
# Гильдии и стороны: (плагин, номер) -> (название, звания по рангам или None)
GUILDS: dict[Key, tuple[str, list[str] | None]] = {
    ("skyrim", 0x048362): ("Соратники", ["новобранец", "соратник", "брат по оружию", "Предвестник"]),
    ("skyrim", 0x0FF234): ("Круг Соратников", None),
    ("skyrim", 0x029DA9): ("Гильдия воров", ["агент", "глава гильдии"]),
    ("skyrim", 0x01F259): ("Коллегия Винтерхолда",
                           ["студент", "ученик", "заклинатель", "учёный", "волшебник", "мастер-волшебник", "архимаг"]),
    ("skyrim", 0x01BDB3): ("Тёмное Братство", None),
    ("skyrim", 0x02BF9A): ("Имперский легион", None),
    ("skyrim", 0x02BF9B): ("Братья Бури", None),
    ("skyrim", 0x072834): ("Клинки", None),
    ("dawnguard", 0x014217): ("Стража Рассвета", None),
    ("dawnguard", 0x003376): ("вампиры Волкихара", None),
}
# Статистика игры -> привычка (подпись). Чего нет в списке — в привычки не идёт.
HABITS = {
    "Locks Picked": "взломано замков", "Pockets Picked": "обчищено карманов", "Items Stolen": "украдено вещей",
    "Horses Stolen": "угнано лошадей", "Sneak Attacks": "атак исподтишка", "Backstabs": "ударов в спину",
    "People Killed": "убито людей", "Animals Killed": "убито зверей", "Creatures Killed": "убито чудищ",
    "Undead Killed": "упокоено нежити", "Daedra Killed": "убито даэдра", "Automatons Killed": "разбито механизмов",
    "Dragon Souls Collected": "поглощено душ драконов", "Words Of Power Learned": "выучено слов силы",
    "Spells Learned": "выучено заклинаний", "Potions Mixed": "сварено зелий", "Poisons Used": "пущено в ход ядов",
    "Weapons Made": "выковано оружия", "Armor Made": "выковано брони", "Magic Items Made": "зачаровано вещей",
    "Persuasions": "убеждений", "Bribes": "взяток", "Intimidations": "запугиваний",
    "Times Jailed": "раз сидел в тюрьме", "Assaults": "нападений", "Murders": "убийств-преступлений",
    "Werewolf Transformations": "превращений в оборотня", "Necks Bitten": "укусов вампиром",
    "Books Read": "прочитано книг", "Dungeons Cleared": "зачищено подземелий",
    "Locations Discovered": "открыто мест",
}
MIN_HABIT = 5        # меньше — ещё не привычка
MAX_HABITS = 6
MAX_QUESTS = 10      # пройденных квестов в сводке


class PlayerProfile:
    """Потокобезопасно: пишут обработчики событий игры, читают сборщики промпта."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = data_dir
        self._lock = threading.Lock()
        self.data: dict = {}               # последний профиль от плагина
        self.stats: dict[str, int] = {}    # статистика игры (накопленная)
        self._baseline = False             # был ли профиль после загрузки сейва (с чем сравнивать)
        self._faction_names: dict[Key, str] | None = None

    # ------------------------------------------------------------ события

    def reset(self) -> None:
        """Загрузили сейв: следующий профиль — новая точка отсчёта (иначе «вышел из гильдии» при старом сейве)."""
        with self._lock:
            self._baseline = False

    def update(self, data: dict, name: str = "Владос") -> list[str]:
        """Новый профиль -> что изменилось (тексты событий)."""
        with self._lock:
            old, self.data = self.data, data
            if not self._baseline:
                self._baseline = True
                return []
        changes: list[str] = []
        old_guilds, new_guilds = self._guilds(old), self._guilds(data)
        for guild, title in new_guilds.items():
            if guild not in old_guilds:
                changes.append(f"{name} вступил: {guild}" + (f" (звание: {title})" if title else ""))
            elif title and title != old_guilds[guild]:
                changes.append(f"{name} получил новое звание — {guild}: {title}")
        for guild in old_guilds.keys() - new_guilds.keys():
            changes.append(f"{name} больше не состоит: {guild}")
        old_quests = {q.get("name") for q in old.get("quests_done", [])}
        for quest in data.get("quests_done", []):
            if quest.get("name") not in old_quests:
                changes.append(f"{name} завершил квест «{quest.get('name')}»")
        old_crime, new_crime = self._crime(old), self._crime(data)
        for hold, gold in new_crime.items():
            if gold > old_crime.get(hold, 0):
                changes.append(f"Розыск во владении {hold}: награда за голову {gold} золотых")
        for hold in old_crime.keys() - new_crime.keys():
            changes.append(f"Розыск во владении {hold} снят")
        if (level := data.get("level")) and old.get("level") and level > old["level"]:
            changes.append(f"{name} достиг {level} уровня")
        return changes

    def stat(self, name: str, value: int) -> None:
        with self._lock:
            self.stats[name] = int(value)

    # ------------------------------------------------------------ сводка

    def summary(self, name: str = "Владос") -> str:
        """«Владос по игре: …» для {memory}. Пусто — профиля ещё нет."""
        with self._lock:
            data, stats = dict(self.data), dict(self.stats)
        if not data:
            return ""
        who = ", ".join(x for x in [data.get("race", ""), "женщина" if data.get("female") else "мужчина",
                                    f"{data['level']} уровень" if data.get("level") else ""] if x)
        lines = [f"{name} по игре: {who}."]
        if data.get("skills"):
            lines.append("Лучшие навыки: " + ", ".join(f"{s['name']} {s['value']}" for s in data["skills"]) + ".")
        guilds = self._guilds(data)
        if guilds:
            lines.append("Состоит: " + ", ".join(g + (f" ({t})" if t else "") for g, t in guilds.items()) + ".")
        crime = self._crime(data)
        if crime:
            lines.append("Награда за голову: " + ", ".join(f"{h} — {g} золотых" for h, g in crime.items()) + ".")
        quests = [q["name"] for q in data.get("quests_done", []) if q.get("name")]
        if quests:
            more = f" и ещё {len(quests) - MAX_QUESTS}" if len(quests) > MAX_QUESTS else ""
            lines.append("Пройдено: " + ", ".join(f"«{q}»" for q in quests[-MAX_QUESTS:]) + more + ".")
        habits = sorted(((v, HABITS[k]) for k, v in stats.items() if k in HABITS and v >= MIN_HABIT), reverse=True)
        if habits:
            lines.append("По делам: " + ", ".join(f"{label} {v}" for v, label in habits[:MAX_HABITS]) + ".")
        return " ".join(lines)

    def guilds(self) -> set[str]:
        """Где Владос состоит («Гильдия воров», «Соратники», …) — по последнему профилю."""
        with self._lock:
            data = dict(self.data)
        return set(self._guilds(data))

    # ------------------------------------------------------------ разбор

    def _guilds(self, data: dict) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for f in data.get("factions", []):
            guild = GUILDS.get((str(f.get("file", "")).rsplit(".", 1)[0].lower(), int(f.get("form", 0)) & 0xFFFFFF))
            if guild:
                title, ranks = guild
                rank = int(f.get("rank", 0))
                out[title] = ranks[rank] if ranks and 0 <= rank < len(ranks) else None
        return out

    def _crime(self, data: dict) -> dict[str, int]:
        names = self._names()
        out: dict[str, int] = {}
        for c in data.get("crime", []):
            key = (str(c.get("file", "")).rsplit(".", 1)[0].lower(), int(c.get("form", 0)) & 0xFFFFFF)
            out[names.get(key, "неизвестное владение")] = int(c.get("gold", 0))
        return out

    def _names(self) -> dict[Key, str]:
        """Названия фракций (у фракций преступлений это владения: «Вайтран», «Рифт»)."""
        with self._lock:
            if self._faction_names is None:
                self._faction_names = {}
                if self.data_dir:
                    try:
                        strings = load_strings(self.data_dir / "Skyrim - Interface.bsa", "strings")
                        records: dict = {b"FACT": {}, b"__people__": {}}
                        for plugin in PLUGINS:
                            if (self.data_dir / f"{plugin}.esm").is_file():
                                _read_plugin(self.data_dir / f"{plugin}.esm", plugin, records, {b"FACT"})
                        for key, (plugin, subs) in records[b"FACT"].items():
                            sid = struct.unpack_from("<I", subs[b"FULL"][0])[0] if b"FULL" in subs else None
                            title = strings.get(plugin, {}).get(sid, "") if sid is not None else ""
                            self._faction_names[key] = title or _edid(subs)
                    except OSError as e:
                        log.warning("Не прочитать фракции игры (%s)", e)
            return self._faction_names
