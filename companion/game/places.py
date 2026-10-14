"""Справка о местах из данных игры: что за место, в каком владении, что в нём есть и кто там.

Источник — записи локаций (LCTN) в Skyrim.esm, Update.esm и DLC: название, родительская локация, ключевые
слова-типы (LocTypeStore — лавка, LocTypeInn — таверна, LocTypeCity — город ...) и уникальные жители
(LCUN/ACUN в записи поселения: NPC -> его локация). Это точные данные игры — выдумывать нечего.
Жителей собираем из всех версий записи: DLC, переопределяя запись (HearthFires — Вайтран), кладёт их в ACUN.
Профессия жителя — по его фракциям-профессиям (JobJarlFaction — ярл, JobMerchantFaction — торговец ...):
житель лавки не обязательно торговец (Назим числится в «Пьяном охотнике»).

Базовые знания Гослинга (production_logs/2026-10-14_память.md, слой 1): он картограф и местный, поэтому
знает устройство поселений Скайрима, даже где ещё не был. Грузится один раз, ~0,5 с.
"""

from __future__ import annotations

import logging
import struct
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from companion.game.dialogue import iter_subrecords
from companion.game.gamefiles import load_strings

log = logging.getLogger(__name__)

PLUGINS = ["skyrim", "update", "dawnguard", "hearthfires", "dragonborn"]  # порядок: поздний переопределяет
WANT = {b"LCTN", b"KYWD", b"NPC_", b"FACT"}
FLAG_COMPRESSED = 0x00040000

Key = tuple[str, int]  # (плагин, где запись создана; номер без индекса мода)

# Типы мест: ключевое слово -> как назвать. Порядок — от главного к второстепенному.
KINDS = {
    "LocTypeHold": "владение", "LocTypeCity": "город", "LocTypeTown": "городок",
    "LocTypeSettlement": "поселение", "LocTypeCastle": "замок", "LocTypeStore": "лавка",
    "LocTypeInn": "таверна", "LocTypeTemple": "храм", "LocTypeGuild": "гильдия",
    "LocTypePlayerHouse": "дом, который можно купить", "LocTypeJail": "тюрьма", "LocTypeCemetery": "кладбище",
    "LocTypeMine": "шахта", "LocTypeFarm": "ферма", "LocTypeLumberMill": "лесопилка",
    "LocTypeMilitaryFort": "форт", "LocTypeMilitaryCamp": "военный лагерь", "LocTypeBanditCamp": "лагерь бандитов",
    "LocTypeForswornCamp": "лагерь изгоев", "LocTypeGiantCamp": "лагерь великанов",
    "LocTypeDragonLair": "логово дракона", "LocTypeDraugrCrypt": "склеп драугров", "LocTypeFalmerHive": "улей фалмеров",
    "LocTypeHagravenNest": "гнездо ворожей", "LocTypeWarlockLair": "логово колдунов",
    "LocTypeVampireLair": "логово вампиров", "LocTypeAnimalDen": "логово зверей", "LocTypeSpriggan": "роща спригганов",
    "LocTypeShip": "корабль", "LocTypeDwarvenAutomatons": "двемерские механизмы",
    "LocSetCave": "пещера", "LocSetCaveIce": "ледяная пещера", "LocSetNordicRuin": "нордские руины",
    "LocSetDwarvenRuin": "двемерские руины", "LocSetMilitaryCamp": "военный лагерь",
    "LocTypeDungeon": "подземелье", "LocTypeHouse": "дом", "LocTypeHabitation": "поселение",
}
SETTLEMENT = {"LocTypeCity", "LocTypeTown", "LocTypeSettlement"}
# Что перечислять в карточке поселения: (ключевое слово, подпись группы)
CARD_GROUPS = [("LocTypeCastle", "замок"), ("LocTypeStore", "лавки"), ("LocTypeInn", "таверна"),
               ("LocTypeTemple", "храмы"), ("LocTypeGuild", "гильдии"),
               ("LocTypePlayerHouse", "дом, который можно купить"), ("LocTypeJail", "тюрьма")]
MAX_RESIDENTS = 4
# Профессии: фракция -> как назвать. Порядок — главная первой (её и пишем перед именем).
JOBS = {
    "JobJarlFaction": "ярл", "JobStewardFaction": "управитель", "JobCourtWizardFaction": "придворный маг",
    "JobHousecarlFaction": "хускарл", "JobGuardCaptainFaction": "начальник стражи", "JobOrcChiefFaction": "вождь",
    "JobOrcWiseWomanFaction": "знахарка", "JobInnkeeperFaction": "трактирщик",
    "JobBlacksmithFaction": "кузнец", "JobApothecaryFaction": "алхимик", "JobSpellFaction": "торговец заклинаниями",
    "JobFletcherFaction": "мастер луков", "JobJewelerFaction": "ювелир", "JobTailorFaction": "портной",
    "JobMerchantFaction": "торговец", "JobStreetVendorFaction": "уличный торговец", "JobPriestFaction": "жрец", "JobBardFaction": "бард",
    "JobInnServer": "прислуга в таверне", "JobCarriageFaction": "возница", "JobHostlerFaction": "конюх",
    "JobAnimalTrainerFaction": "торговец лошадьми", "JobFarmerFaction": "фермер", "JobMinerFaction": "шахтёр",
    "JobLumberjackFaction": "лесоруб",
}
COURT = {"JobJarlFaction", "JobStewardFaction", "JobCourtWizardFaction", "JobHousecarlFaction",
         "JobGuardCaptainFaction", "JobOrcChiefFaction", "JobOrcWiseWomanFaction"}
# Наставники: JobTrainer<навык>Faction -> чему учит.
TRAINERS = {
    "Alchemy": "алхимии", "Alteration": "изменению", "Block": "блоку", "Conjuration": "колдовству",
    "Destruction": "разрушению", "Enchanting": "зачарованию", "HeavyArmor": "тяжёлой броне", "Illusion": "иллюзии",
    "LightArmor": "лёгкой броне", "Lockpicking": "взлому", "Marksman": "стрельбе", "OneHanded": "одноручному оружию",
    "Pickpocket": "карманным кражам", "Restoration": "восстановлению", "Smithing": "кузнечному делу",
    "Sneak": "скрытности", "Speechcraft": "красноречию", "TwoHanded": "двуручному оружию",
}
MAX_CASTLE_RESIDENTS = 12  # в замке живёт ярл — не прятать его в «и ещё N»


@dataclass
class Place:
    key: Key
    edid: str
    name: str
    parent: Key | None = None
    keywords: list[str] = field(default_factory=list)
    residents: list[str] = field(default_factory=list)  # с профессией — первыми
    staff: int = 0  # сколько первых из residents — с профессией

    def kinds(self) -> list[str]:
        labels = [KINDS[k] for k in KINDS if k in self.keywords]
        if "город" in labels or "городок" in labels:
            labels = [x for x in labels if x != "поселение"]
        return [x for i, x in enumerate(labels) if x not in labels[:i]]  # без повторов

    def has(self, keyword: str) -> bool:
        return keyword in self.keywords


class PlaceBook:
    """Справочник мест. Грузится при первом обращении; потокобезопасно."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._lock = threading.Lock()
        self._places: dict[Key, Place] | None = None
        self._by_name: dict[str, list[Place]] = {}
        self._children: dict[Key, list[Place]] = {}

    # ------------------------------------------------------------ поиск

    def find(self, name: str) -> Place | None:
        """Место по точному названию (без учёта регистра). Одноимённых несколько — главнее то, что
        поселение, потом то, у чего есть вложенные места."""
        found = self._index()[1].get(name.strip().casefold(), [])
        if not found:
            return None
        return max(found, key=lambda p: (any(p.has(k) for k in SETTLEMENT), len(self._children.get(p.key, []))))

    def search(self, part: str, limit: int = 6) -> list[Place]:
        part = part.strip().casefold()
        places, _ = self._index()
        return [p for p in places.values() if part and part in p.name.casefold()][:limit]

    def parent(self, place: Place) -> Place | None:
        return self._index()[0].get(place.parent) if place.parent else None

    def settlement_of(self, place: Place) -> Place | None:
        """Поселение, в котором это место (или оно само)."""
        for p in self._chain(place):
            if any(p.has(k) for k in SETTLEMENT):
                return p
        return None

    def hold_of(self, place: Place) -> Place | None:
        return next((p for p in self._chain(place) if p.has("LocTypeHold")), None)

    def _chain(self, place: Place):
        seen: set[Key] = set()
        p: Place | None = place
        while p and p.key not in seen:
            yield p
            seen.add(p.key)
            p = self.parent(p)

    # ------------------------------------------------------------ карточка

    def card(self, name: str) -> str:
        """Текст для модели: что за место и что в нём есть. "" — такого места в справочнике нет."""
        place = self.find(name)
        if not place:
            return ""
        town = self.settlement_of(place)
        head = town or place
        hold = self.hold_of(head)
        title = f"{head.name} — {', '.join(head.kinds()) or 'место'}"
        if hold and hold.key != head.key:
            title += f", владение {hold.name.removeprefix('Владение ')}"
        lines = [title + "."]
        if place.key != head.key:
            lines.append(f"Сейчас вы в: {self._describe(place)}.")
        groups = []
        children = self._inside(head)
        for keyword, label in CARD_GROUPS:
            merged: dict[str, Place] = {}  # одноимённые (Йоррваскр — зал и жилые комнаты) — одной строкой
            for c in children:
                if c.has(keyword):
                    m = merged.setdefault(c.name, Place(c.key, c.edid, c.name, c.parent, c.keywords, []))
                    staff = [r for r in c.residents[:c.staff] if r not in m.residents]
                    m.residents = m.residents[:m.staff] + staff + m.residents[m.staff:]
                    m.staff += len(staff)
                    m.residents += [r for r in c.residents if r not in m.residents]
            items = [self._describe(m, kinds=False) for m in merged.values()]
            if items:
                groups.append(f"{label} — {', '.join(items)}")
        if groups:
            lines.append("Там есть: " + "; ".join(groups) + ".")
        elif head.residents:
            lines.append("Там живут: " + ", ".join(head.residents[:MAX_RESIDENTS]) + ".")
        return " ".join(lines)

    def _describe(self, place: Place, kinds: bool = True) -> str:
        """«Товары Белетора (лавка; Белетор, Сигурд)»."""
        details = []
        if kinds and place.kinds():
            details.append(", ".join(k for k in place.kinds() if k != "дом" or len(place.kinds()) == 1))
        people = place.residents
        if place.has("LocTypeStore") and place.staff:  # в лавке — те, кто торгует (Назим живёт, но не торгует)
            people = people[:place.staff]
        if people:
            limit = MAX_CASTLE_RESIDENTS if place.has("LocTypeCastle") else MAX_RESIDENTS
            more = len(people) - limit
            details.append(", ".join(people[:limit]) + (f" и ещё {more}" if more > 0 else ""))
        return f"{place.name} ({'; '.join(details)})" if details else place.name

    def _inside(self, head: Place, depth: int = 2) -> list[Place]:
        """Места внутри поселения — на depth уровней вглубь (у Солитьюда лавки вложены глубже),
        но не заходя в другие поселения."""
        found, level = [], [head]
        for _ in range(depth):
            level = [c for p in level for c in self._children.get(p.key, [])
                     if not any(c.has(k) for k in SETTLEMENT)]
            found += level
        return found

    # ------------------------------------------------------------ загрузка

    def _index(self) -> tuple[dict[Key, Place], dict[str, list[Place]]]:
        with self._lock:
            if self._places is None:
                self._places = {}
                try:
                    self._load()
                except OSError as e:
                    log.warning("Не прочитать локации игры (%s) — справки о местах не будет", e)
                for p in self._places.values():
                    self._by_name.setdefault(p.name.casefold(), []).append(p)
                    if p.parent:
                        self._children.setdefault(p.parent, []).append(p)
                log.info("Справка о местах: %d мест", len(self._places))
            return self._places, self._by_name

    def _load(self) -> None:
        strings = load_strings(self.data_dir / "Skyrim - Interface.bsa", "strings")
        records: dict[bytes, dict] = {t: {} for t in WANT} | {b"__people__": {}}
        for plugin in PLUGINS:
            path = self.data_dir / f"{plugin}.esm"
            if path.is_file():
                _read_plugin(path, plugin, records)

        def name(entry: tuple[str, dict] | None) -> str:
            if not entry or b"FULL" not in entry[1]:
                return ""
            plugin, subs = entry
            sid = struct.unpack_from("<I", subs[b"FULL"][0])[0]
            return strings.get(plugin, {}).get(sid) or strings.get("skyrim", {}).get(sid, "")

        keywords = {key: _edid(subs) for key, (_, subs) in records[b"KYWD"].items()}
        factions = {key: _edid(subs) for key, (_, subs) in records[b"FACT"].items()}

        def person(key: Key) -> tuple[str, bool]:
            """«торговец Белетор (учит красноречию)» и есть ли у него профессия."""
            entry = records[b"NPC_"].get(key)
            who = name(entry)
            if not entry or not who:
                return who, False
            resolve = entry[1][b"__resolve__"][0]
            # SNAM: фракция + ранг. Ранг −1 в игре ставят и настоящим профессиям (Белетор — торговец), и «запасным»
            # (Хронгар и Вигнар во фракции ярла, как и Балгруф). Поэтому придворные роли — только с рангом ≥ 0,
            # ремесло — сначала ≥ 0 (Микаэль — бард, а не трактирщик), иначе и −1.
            ranked = [(factions.get(resolve(form), ""), rank) for form, rank in
                      (struct.unpack_from("<Ib", blob) for blob in entry[1].get(b"SNAM", []))]
            edids = [f for f, rank in ranked if rank >= 0]
            jobs = [JOBS[f] for f in JOBS if f in edids]
            if not jobs:
                jobs = [JOBS[f] for f in JOBS if f not in COURT and f in {f for f, _ in ranked}]
            teaches = [TRAINERS[f[10:-7]] for f in edids
                       if f.startswith("JobTrainer") and f.endswith("Faction") and f[10:-7] in TRAINERS]
            text = f"{jobs[0]} {who}" if jobs else who
            if teaches:
                text += f" (учит {', '.join(teaches)})"
            return text, bool(jobs or teaches)

        for key, entry in records[b"LCTN"].items():
            plugin, subs = entry
            title = name(entry)
            if not title:
                continue
            resolve = subs[b"__resolve__"][0]
            self._places[key] = Place(
                key=key, edid=_edid(subs), name=title,
                parent=resolve(struct.unpack_from("<I", subs[b"PNAM"][0])[0]) if b"PNAM" in subs else None,
                keywords=[keywords.get(resolve(k), "") for blob in subs.get(b"KWDA", [])
                          for (k,) in struct.iter_unpack("<I", blob)])
        # Жители: LCUN/ACUN поселения (во всех версиях записи) — (NPC, ссылка, локация NPC).
        # С профессией — первыми: в карточку попадают первые несколько.
        people: dict[Key, dict[str, bool]] = {}
        for key, versions in records[b"__people__"].items():
            for resolve, blob in versions:
                for npc, _ref, loc in struct.iter_unpack("<III", blob[:len(blob) // 12 * 12]):
                    place = self._places.get(resolve(loc)) or self._places.get(key)
                    who, has_job = person(resolve(npc))
                    if place and who:
                        people.setdefault(place.key, {}).setdefault(who, has_job)
        for key, found in people.items():
            self._places[key].residents = sorted(found, key=lambda who: not found[who])
            self._places[key].staff = sum(found.values())


def _edid(subs: dict) -> str:
    return subs.get(b"EDID", [b""])[0].rstrip(b"\0").decode("utf-8", "replace")


def _read_plugin(path: Path, plugin: str, records: dict[bytes, dict[Key, tuple[str, dict]]],
                 want: set[bytes] | None = None) -> None:
    """Записи WANT из плагина -> records[тип][(плагин-создатель, номер)] = (этот плагин, подзаписи).
    Номер формы: старший байт — индекс в списке мастеров плагина (или сам плагин)."""
    want = want or WANT
    data = path.read_bytes()
    header_size = 24 + struct.unpack_from("<I", data, 4)[0]
    masters = [Path(p.rstrip(b"\0").decode("utf-8", "replace")).stem.lower()
               for st, p in iter_subrecords(data[24:header_size]) if st == b"MAST"]

    def resolve(form: int) -> Key:
        index = form >> 24
        return (masters[index] if index < len(masters) else plugin, form & 0xFFFFFF)

    def walk(start: int, end: int) -> None:
        pos = start
        while pos < end:
            typ = data[pos:pos + 4]
            size = struct.unpack_from("<I", data, pos + 4)[0]
            if typ == b"GRUP":
                if struct.unpack_from("<i", data, pos + 12)[0] == 0 and data[pos + 8:pos + 12] in want:
                    walk(pos + 24, pos + size)
                pos += size
                continue
            flags, form = struct.unpack_from("<II", data, pos + 8)
            body = data[pos + 24:pos + 24 + size]
            if flags & FLAG_COMPRESSED:
                body = zlib.decompress(body[4:])
            subs: dict = {b"__resolve__": [resolve]}
            for st, payload in iter_subrecords(body):
                subs.setdefault(st, []).append(payload)
            records[typ][resolve(form)] = (plugin, subs)
            if typ == b"LCTN":
                people = b"".join(subs.get(b"LCUN", []) + subs.get(b"ACUN", []))
                records.setdefault(b"__people__", {}).setdefault(resolve(form), []).append((resolve, people))
            pos += 24 + size

    walk(header_size, len(data))
