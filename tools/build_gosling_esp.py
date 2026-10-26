"""Собирает мод «Gosling» — NPC-компаньон «Гослинг» — без Creation Kit.

Gosling.esp:
- NPC «Гослинг» — копия записи HirelingErikTheSlayer (Эрик-наёмник) из Skyrim.esm: лицо, раса, голос
  MaleYoungEager — его; правим имя, фракции компаньона, пакет поведения (бродит по таверне), флаги
  (бессмертен — essential), уровень.
  Без шаблона (Use Traits): с ним игра собирает «составного» NPC с другим итоговым типом голоса, и реплики
  из нашего .wav не звучат. Файл лица (FaceGen) копируем из архивов игры под номером Гослинга.
  Личные реплики Эрика привязаны к нему самому — к Гослингу не переходят.
- Стоит в таверне «Морозный фрукт» в Рорикстеде, рядом с местом Эрика; отношение к игроку — союзник
  (в диалоге есть «Следуй за мной»).
- Квест GoslingVQ (стартует с игрой) с двумя темами-заглушками: голос из уст Гослинга (кусок 5б).
- Квест GoslingTravelQuest и пакет GoslingTravelPackage (копия ванильного «путешествуй к алиасу»): поход к месту
  по приказу голосом (go_to) — алиасы заполняет скрипт GoslingOrders; пакет GoslingLeadPackage (копия ванильного
  «сопроводи к месту») — режим «Веди»: ведёт игрока и ждёт, если тот отстал; пакет GoslingApproachPackage (копия
  ванильного «следуй за…») — подойти к человеку и держаться в паре шагов, пока он ходит; пакет GoslingLingerPackage
  (копия ванильного Sandbox) — побыть на месте: постоять, посидеть, послушать, о чём говорят вокруг.
  Звук реплик сервис кладёт в Sound\\Voice\\Gosling.esp\\MaleYoungEager\\ перед каждой фразой.
Ещё в моде: Scripts\\GoslingVoice.pex и GoslingOrders.pex (компилируются из mod/Scripts/Source), Seq\\Gosling.seq (без него
реплики квеста, стартующего с игрой, не звучат), заглушки .wav и .lip для тем
и файлы лица Гослинга (meshes/textures\\...\\FaceGenData\\...\\Gosling.esp\\00000800.*).

    python tools/build_gosling_esp.py              # -> build/mod/
    python tools/build_gosling_esp.py --install    # + копия в MO2 (npc_voice.mod_dir в config.yaml)

Готовый мод в git не кладём: в .esp данные из Skyrim.esm. Он всегда пересобирается этим скриптом.
"""

from __future__ import annotations

import argparse
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from companion import gosling_mod as mod  # noqa: E402
from companion.config import load_config  # noqa: E402
from companion.game.gamefiles import load_strings, read_bsa_index, read_file  # noqa: E402
from companion.game.voices import voice_types  # noqa: E402

OUT = ROOT / "build" / "mod"
SCRIPTS_SRC = ROOT / "mod" / "Scripts" / "Source"

NAME = "Гослинг"
FORM_VERSION = 44  # версия записей Skyrim SE

# Записи Skyrim.esm (найдены по EditorID, проверено: Update.esm их не меняет)
TEMPLATE_NPC = 0x00065657       # HirelingErikTheSlayer
INN_CELL = 0x00013870           # RoriksteadFrostfruitInn
ERIK_IN_INN = 0x000350B8        # где стоит Эрик в таверне
POTENTIAL_FOLLOWER = 0x0005C84D  # PotentialFollowerFaction
CURRENT_FOLLOWER = 0x0005C84E    # CurrentFollowerFaction
SANDBOX_PACKAGE = 0x0001B217    # DefaultSandboxEditorLocation512 — бродит в радиусе ~7 м от места
TRAVEL_SAMPLE = 0x001065F9      # WERoad08Travel: пакет «путешествуй к алиасу» (шаблон Travel) без скриптов и условий
LEAD_SAMPLE = 0x00074995        # TG00SPBrandSheiKeepMarch: «сопроводи к месту» (шаблон Escort) без скриптов и условий
FOLLOW_SAMPLE = 0x00106A5C      # WERoad11Follow: «следуй за алиасом» (шаблон Follow) без скриптов и условий
LINGER_SAMPLE = 0x000BA039      # WE06PostEncounterSandbox: «будь рядом с алиасом» (шаблон Sandbox) без скриптов и условий
PLAYER_REF = 0x00000014         # PlayerRef — кого сопровождать (как в dunCGRalofToTowerMarker01a)
PLAYER = 0x00000007

# Поход к месту (приказ go_to) — квест TRAVEL_QUEST с алиасами и пакетом по образцу TRAVEL_SAMPLE.
TRAVEL_PRIORITY = 70  # выше квеста спутника DialogueFollower (50): пакет похода перебивает следование за игроком
TRAVEL_RADIUS = 500   # единиц (~7 м) — дошёл: маркер карты может стоять вне навмеша
PREFERRED_SPEED, RUN = 0x2000, 2  # PKDT: флаг «своя скорость» и скорость (0 шаг, 1 трусцой, 2 бегом, 3 быстрый шаг)
# PKDT, флаги прерываний (uint16 в байтах 8–9): что может прервать пакет. В походе и «Веди» снимаем: приветствие
# игрока (0x1), случайные разговоры (0x2), реакции на действия игрока (0x10), болтовню (0x80) — иначе Гослинг
# останавливался, когда Владос подходил, и говорил записанными фразами Эрика (21.10). Бой и прочее — как у образца.
NO_CHATTER = 0x1 | 0x2 | 0x10 | 0x80


def quiet_package_data(payload: bytes, speed: int | None = None) -> bytes:
    """PKDT без прерываний на приветствия и болтовню; speed — своя скорость (с флагом PREFERRED_SPEED)."""
    flags, interrupts = struct.unpack_from("<I", payload)[0], struct.unpack_from("<H", payload, 8)[0]
    out = bytearray(payload)
    if speed is not None:
        struct.pack_into("<I", out, 0, flags | PREFERRED_SPEED)
        out[6] = speed
    struct.pack_into("<H", out, 8, interrupts & ~NO_CHATTER)
    return bytes(out)
LOCATION_ALIAS = 8    # PLDT: место — алиас-ссылка квеста
ALIAS_OPTIONAL, ALIAS_REUSE = 0x2, 0x8  # FNAM алиаса: пустой не мешает квесту стартовать; заполняет скрипт
TARGET_REF = 0       # PTDA: цель — конкретная ссылка
TARGET_ALIAS = 4     # PTDA: цель — алиас-ссылка квеста
# Подойти к человеку: держаться от него в паре шагов (единиц; у образца 256–512 — стоял бы в 4–7 м, разговаривать далеко).
APPROACH_MIN, APPROACH_MAX = 100.0, 200.0
LINGER_RADIUS = 700  # единиц (~10 м) — побыть здесь: зал таверны, площадь перед ним
# Входы Sandbox (Bool) по порядку после места: снять у образца сон (спать в чужой кровати посреди вылазки) и разговоры
# (заговаривал бы с NPC записанными фразами Эрика — их снимаем и в походе, NO_CHATTER).
LINGER_OFF = {2: "сон", 3: "разговоры"}
LEAD_WAIT = 1400.0   # единиц (~20 м): игрок отстал дальше — «Веди» ждёт его (у образца 512 — останавливался бы на каждом шаге)


def own(local_id: int) -> int:
    """Своя запись: старший байт 01 = «этот файл» (после одного мастера, Skyrim.esm)."""
    return 0x01000000 | local_id


COMPRESSED = 0x00040000
PERSISTENT = 0x00000400

# ACBS flags
ESSENTIAL, RESPAWN, UNIQUE, PC_LEVEL_MULT, PROTECTED = 0x2, 0x8, 0x20, 0x80, 0x800

# Файлы лица: (архив, путь в архиве по образцу, путь в моде для Гослинга)
FACEGEN = [
    ("Skyrim - Meshes0.bsa", r"meshes\actors\character\facegendata\facegeom\skyrim.esm",
     r"meshes\actors\character\FaceGenData\FaceGeom\Gosling.esp", "nif"),
    ("Skyrim - Textures0.bsa", r"textures\actors\character\facegendata\facetint\skyrim.esm",
     r"textures\actors\character\FaceGenData\FaceTint\Gosling.esp", "dds"),
]


# ------------------------------------------------------------------ чтение Skyrim.esm

def subrecords(body: bytes) -> list[tuple[bytes, bytes]]:
    out, pos, big = [], 0, None
    while pos < len(body):
        typ = body[pos:pos + 4]
        size = struct.unpack_from("<H", body, pos + 4)[0]
        pos += 6
        if big is not None:
            size, big = big, None
        if typ == b"XXXX":
            big = struct.unpack_from("<I", body, pos)[0]
            pos += size
            continue
        out.append((typ, body[pos:pos + size]))
        pos += size
    return out


def find_records(esm: Path, forms: set[int]) -> dict[int, list[tuple[bytes, bytes]]]:
    """formID -> подзаписи (распакованные)."""
    data = esm.read_bytes()
    found = {}

    def walk(start: int, end: int) -> None:
        pos = start
        while pos < end and len(found) < len(forms):
            size = struct.unpack_from("<I", data, pos + 4)[0]
            if data[pos:pos + 4] == b"GRUP":
                walk(pos + 24, pos + size)
                pos += size
            else:
                flags, form = struct.unpack_from("<II", data, pos + 8)
                if form in forms:
                    body = data[pos + 24:pos + 24 + size]
                    found[form] = subrecords(zlib.decompress(body[4:]) if flags & COMPRESSED else body)
                pos += 24 + size

    walk(24 + struct.unpack_from("<I", data, 4)[0], len(data))
    missing = forms - found.keys()
    if missing:
        raise SystemExit(f"Нет записей в {esm.name}: {[f'{f:08X}' for f in missing]}")
    return found


# ------------------------------------------------------------------ запись .esp

class Writer:
    """Записи и группы .esp; считает их для заголовка (HEDR)."""

    def __init__(self) -> None:
        self.count = 0

    def record(self, typ: bytes, form: int, subs: list[tuple[bytes, bytes]], flags: int = 0) -> bytes:
        self.count += 1
        body = b"".join(sub(t, p) for t, p in subs)
        return struct.pack("<4sIIIIHH", typ, len(body), flags, form, 0, FORM_VERSION, 0) + body

    def group(self, label: bytes, gtype: int, content: bytes) -> bytes:
        self.count += 1
        return struct.pack("<4sI4siHHI", b"GRUP", 24 + len(content), label, gtype, 0, 0, 0) + content


def zstring(text: str) -> bytes:
    return text.encode("utf-8") + b"\0"  # русские строки игры — в UTF-8


def sub(typ: bytes, payload: bytes) -> bytes:
    return typ + struct.pack("<H", len(payload)) + payload


def u32(value: int) -> bytes:
    return struct.pack("<I", value)


# ------------------------------------------------------------------ Гослинг

def gosling_npc(w: Writer, template: list[tuple[bytes, bytes]]) -> bytes:
    out: list[tuple[bytes, bytes]] = []
    factions_done = package_done = False
    for typ, payload in template:
        if typ == b"EDID":
            out.append((typ, zstring("GoslingNPC")))
        elif typ == b"VMAD":
            continue  # скрипты Эрика не нужны
        elif typ == b"ACBS":
            (flags, magicka, stamina, level, cmin, cmax, speed, disp, tflags,
             health, bleedout) = struct.unpack("<IhhHHHHhHhH", payload)
            # Бессмертен: в бою падает на колено и встаёт после боя (защищённого мог убить сам игрок).
            flags = (flags | UNIQUE | ESSENTIAL | PC_LEVEL_MULT) & ~(PROTECTED | RESPAWN)
            out.append((typ, struct.pack("<IhhHHHHhHhH", flags, magicka, stamina, 1000, 1, 50, speed,
                                         disp, 0, health, bleedout)))  # без шаблона
        elif typ == b"SNAM":  # фракции Эрика -> только фракции компаньона
            if not factions_done:
                out.append((typ, struct.pack("<Ib3x", POTENTIAL_FOLLOWER, 0)))
                out.append((typ, struct.pack("<Ib3x", CURRENT_FOLLOWER, -1)))
                factions_done = True
        elif typ == b"TPLT":
            continue
        elif typ == b"PKID":  # распорядок Эрика -> бродить у места, где поставлен
            if not package_done:
                out.append((typ, u32(SANDBOX_PACKAGE)))
                package_done = True
        elif typ in (b"FULL", b"SHRT"):  # в Skyrim.esm это номера строк, у нас — сам текст
            out.append((typ, zstring(NAME)))
        else:
            out.append((typ, payload))
    if not package_done:
        idx = next(i for i, (t, _) in enumerate(out) if t == b"AIDT") + 1
        out.insert(idx, (b"PKID", u32(SANDBOX_PACKAGE)))
    return w.record(b"NPC_", own(mod.NPC), out)


def relationship(w: Writer) -> bytes:
    # Как у ванильных NPC: родитель — NPC, ребёнок — игрок, ранг 1 (союзник), без типа связи.
    return w.record(b"RELA", own(mod.RELATIONSHIP),
                    [(b"DATA", struct.pack("<IIhBBI", own(mod.NPC), PLAYER, 1, 0, 0, 0))])


def inn_with_gosling(w: Writer, cell: list[tuple[bytes, bytes]], erik_ref: list[tuple[bytes, bytes]],
                     strings: dict[int, str]) -> bytes:
    """Таверна (копия записи ячейки) + Гослинг в ней. Интерьер: блок = последняя цифра
    formID в десятичной записи, подблок — предпоследняя."""
    subs = []
    for typ, payload in cell:
        if typ == b"FULL":  # номер строки -> текст (у нас нелокализованный плагин)
            payload = zstring(strings.get(struct.unpack("<I", payload)[0], "Морозный фрукт"))
        subs.append((typ, payload))
    cell_rec = w.record(b"CELL", INN_CELL, subs)

    x, y, z, rx, ry, rz = struct.unpack("<6f", dict(erik_ref)[b"DATA"])
    # В 80 единицах (~1 м) перед Эриком, лицом к нему.
    pos = (x + 80 * math.sin(rz), y + 80 * math.cos(rz), z)
    ref = w.record(b"ACHR", own(mod.NPC_REF), [
        (b"NAME", u32(own(mod.NPC))),
        (b"DATA", struct.pack("<6f", *pos, 0.0, 0.0, (rz + math.pi) % (2 * math.pi))),
    ], flags=PERSISTENT)

    label = u32(INN_CELL)
    children = w.group(label, 6, w.group(label, 8, ref))
    block, subblock = INN_CELL % 10, (INN_CELL // 10) % 10
    return w.group(b"CELL", 0, w.group(struct.pack("<i", block), 2,
                                       w.group(struct.pack("<i", subblock), 3, cell_rec + children)))


def voice_topics(w: Writer) -> bytes:
    """Темы-заглушки — как у Mantella: тип Misc / StandOnFurniture (STOF), без ветки диалога,
    одна реплика «...» без условий (её может сказать кто угодно, звук — по типу голоса говорящего)."""
    content = b""
    for topic, info, edid in mod.LINES + mod.NPC_LINES:
        content += w.record(b"DIAL", own(topic), [
            (b"EDID", zstring(edid)),
            (b"PNAM", struct.pack("<f", 50.0)),           # приоритет
            (b"QNAM", u32(own(mod.VOICE_QUEST))),
            (b"DATA", bytes([0, 7, 0x56, 0])),            # флаги, категория Misc, подтип — как в Skyrim.esm
            (b"SNAM", b"STOF"),
            (b"TIFC", u32(1)),                            # число реплик
        ])
        content += w.group(u32(own(topic)), 7, w.record(b"INFO", own(info), [
            (b"ENAM", struct.pack("<HH", 0, 0)),
            (b"CNAM", b"\0"),
            # эмоция neutral 50, ответ №1, без звукового файла, флаг «анимация эмоции»
            (b"TRDT", struct.pack("<IIIB3xIB3x", 0, 50, 0, 1, 0, 1)),
            (b"NAM1", zstring("...")),
            (b"NAM2", b"\0"),
            (b"NAM3", b"\0"),
        ]))
    return w.group(b"DIAL", 0, content)


def voice_type(w: Writer) -> bytes:
    # Как ванильный MaleYoungEager: имя + DNAM 0x01 (разрешён общий диалог).
    return w.group(b"VTYP", 0, w.record(b"VTYP", own(mod.VOICE_TYPE_ID), [
        (b"EDID", zstring(mod.VOICE_TYPE)),
        (b"DNAM", bytes([0x01])),
    ]))


def voice_quest(w: Writer) -> bytes:
    # Флаги 0x11 (стартует с игрой, …) и приоритет 90 — как у ванильных диалоговых квестов без скриптов.
    return w.record(b"QUST", own(mod.VOICE_QUEST), [
        (b"EDID", zstring(mod.QUEST_EDID)),
        (b"DNAM", struct.pack("<HBBII", 0x11, 90, 0, 0, 0)),
        (b"NEXT", b""),
        (b"ANAM", u32(0)),
    ])


def travel_quest(w: Writer) -> bytes:
    """Квест похода: не стартует с игрой — его запускает скрипт GoslingOrders.GoTo и сам заполняет алиасы
    (ForceRefTo): «Путник» — Гослинг, с пакетом похода; «Цель» — маркер карты. Алиасы — как в ванильных квестах
    (weroad08): ALST номер, ALID имя, FNAM флаги, ALPC пакеты, VTCK, ALED."""
    def alias(alias_id: int, name: str, packages: tuple[int, ...] = ()) -> list[tuple[bytes, bytes]]:
        return [(b"ALST", u32(alias_id)), (b"ALID", zstring(name)), (b"FNAM", u32(ALIAS_OPTIONAL | ALIAS_REUSE)),
                *[(b"ALPC", u32(p)) for p in packages], (b"VTCK", u32(0)), (b"ALED", b"")]

    return w.record(b"QUST", own(mod.TRAVEL_QUEST), [
        (b"EDID", zstring(mod.TRAVEL_QUEST_EDID)),
        (b"DNAM", struct.pack("<HBBII", 0, TRAVEL_PRIORITY, 0, 0, 0)),
        (b"NEXT", b""),
        (b"ANAM", u32(5)),  # номер следующего алиаса
        *alias(mod.TRAVELER_ALIAS, "Traveler", (own(mod.TRAVEL_PACKAGE),)),
        *alias(mod.DESTINATION_ALIAS, "Destination"),
        *alias(mod.LEADER_ALIAS, "Leader", (own(mod.LEAD_PACKAGE),)),
        *alias(mod.APPROACHER_ALIAS, "Approacher", (own(mod.APPROACH_PACKAGE),)),
        *alias(mod.LINGERER_ALIAS, "Lingerer", (own(mod.LINGER_PACKAGE),)),
    ])


def lead_package(w: Writer, sample: list[tuple[bytes, bytes]]) -> bytes:
    """Пакет «Веди» — копия ванильного LEAD_SAMPLE (шаблон Escort: кого, куда, «ждать отставших дальше…»): свой
    квест-владелец, сопровождает игрока, место — алиас «Цель», ждёт, если игрок отстал дальше LEAD_WAIT.
    Входы шаблона по номерам (UNAM): 0b — кого, 03 — куда, 04 — дистанция ожидания (Float), дальше — как у образца."""
    out, patched = [], set()
    for typ, payload in sample:
        name = typ
        if typ == b"EDID":
            payload = zstring("GoslingLeadPackage")
        elif typ == b"PKDT":  # бегом: образец — быстрым шагом, Гослинг «медленно шёл» к месту (21.10)
            payload = quiet_package_data(payload, RUN)
        elif typ == b"QNAM":
            payload = u32(own(mod.TRAVEL_QUEST))
        elif typ == b"PTDA":
            payload = struct.pack("<iIi", TARGET_REF, PLAYER_REF, 0)
        elif typ == b"PLDT":
            payload = struct.pack("<iIi", LOCATION_ALIAS, mod.DESTINATION_ALIAS, TRAVEL_RADIUS)
        elif typ == b"CNAM" and b"PLDT" in patched and b"WAIT" not in patched:
            name = b"WAIT"  # первый вход после места — Float «Distance to Wait for Follower(s)»
            payload = struct.pack("<f", LEAD_WAIT)
        else:
            out.append((typ, payload))
            continue
        patched.add(name)
        out.append((typ, payload))
    if patched != {b"EDID", b"PKDT", b"QNAM", b"PTDA", b"PLDT", b"WAIT"}:
        raise SystemExit(f"Образец пакета {LEAD_SAMPLE:08X} не такой, как ждали: есть только {sorted(patched)}")
    return w.record(b"PACK", own(mod.LEAD_PACKAGE), out)


def approach_package(w: Writer, sample: list[tuple[bytes, bytes]]) -> bytes:
    """Пакет «подойти к человеку» — копия ванильного FOLLOW_SAMPLE (шаблон Follow: за кем, мин. и макс. радиус,
    «сопровождать»): свой квест-владелец, за кем — алиас «Цель», в паре шагов (APPROACH_MIN..APPROACH_MAX).
    Человек идёт — Гослинг идёт за ним; стоит — стоит рядом. Скорость — как у образца: бегом он наскакивал бы на него."""
    out, patched, radius = [], set(), [APPROACH_MIN, APPROACH_MAX]
    for typ, payload in sample:
        name = typ
        if typ == b"EDID":
            payload = zstring("GoslingApproachPackage")
        elif typ == b"PKDT":
            payload = quiet_package_data(payload)
        elif typ == b"QNAM":
            payload = u32(own(mod.TRAVEL_QUEST))
        elif typ == b"PTDA":
            payload = struct.pack("<iIi", TARGET_ALIAS, mod.DESTINATION_ALIAS, 0)
        elif typ == b"CNAM" and b"PTDA" in patched and radius:
            name = b"MIN" if len(radius) == 2 else b"MAX"  # два Float после цели — «Min Radius», «Max Radius»
            payload = struct.pack("<f", radius.pop(0))
        else:
            out.append((typ, payload))
            continue
        patched.add(name)
        out.append((typ, payload))
    if patched != {b"EDID", b"PKDT", b"QNAM", b"PTDA", b"MIN", b"MAX"}:
        raise SystemExit(f"Образец пакета {FOLLOW_SAMPLE:08X} не такой, как ждали: есть только {sorted(patched)}")
    return w.record(b"PACK", own(mod.APPROACH_PACKAGE), out)


def linger_package(w: Writer, sample: list[tuple[bytes, bytes]]) -> bytes:
    """Пакет «побыть здесь» — копия ванильного LINGER_SAMPLE (шаблон Sandbox: место, что можно делать): свой
    квест-владелец, место — алиас «Цель» (метка, где он стоит) в радиусе LINGER_RADIUS; без сна и разговоров (LINGER_OFF)."""
    out, patched, flags = [], set(), 0
    for typ, payload in sample:
        name = typ
        if typ == b"EDID":
            payload = zstring("GoslingLingerPackage")
        elif typ == b"PKDT":
            payload = quiet_package_data(payload)
        elif typ == b"QNAM":  # владелец пакета и квест у условий его процедур — свой
            payload = u32(own(mod.TRAVEL_QUEST))
        elif typ == b"PLDT":
            payload = struct.pack("<iIi", LOCATION_ALIAS, mod.DESTINATION_ALIAS, LINGER_RADIUS)
        elif typ == b"CNAM" and b"PLDT" in patched and len(payload) == 1:
            flags += 1
            if flags - 1 not in LINGER_OFF:
                out.append((typ, payload))
                continue
            name, payload = LINGER_OFF[flags - 1].encode(), bytes([0])
        else:
            out.append((typ, payload))
            continue
        patched.add(name)
        out.append((typ, payload))
    want = {b"EDID", b"PKDT", b"QNAM", b"PLDT"} | {x.encode() for x in LINGER_OFF.values()}
    if patched != want:
        raise SystemExit(f"Образец пакета {LINGER_SAMPLE:08X} не такой, как ждали: есть только {sorted(patched)}")
    return w.record(b"PACK", own(mod.LINGER_PACKAGE), out)


def travel_package(w: Writer, sample: list[tuple[bytes, bytes]]) -> bytes:
    """Пакет похода — копия ванильного TRAVEL_SAMPLE (шаблон Travel: место, «верхом» нет, «по дорогам» да):
    свой квест-владелец, место — алиас «Цель», бегом."""
    out, patched = [], set()
    for typ, payload in sample:
        if typ == b"EDID":
            payload = zstring("GoslingTravelPackage")
        elif typ == b"PKDT":
            payload = quiet_package_data(payload, RUN)
        elif typ == b"QNAM":
            payload = u32(own(mod.TRAVEL_QUEST))
        elif typ == b"PLDT":
            payload = struct.pack("<iIi", LOCATION_ALIAS, mod.DESTINATION_ALIAS, TRAVEL_RADIUS)
        else:
            out.append((typ, payload))
            continue
        patched.add(typ)
        out.append((typ, payload))
    if patched != {b"EDID", b"PKDT", b"QNAM", b"PLDT"}:
        raise SystemExit(f"Образец пакета {TRAVEL_SAMPLE:08X} не такой, как ждали: есть только {sorted(patched)}")
    return w.record(b"PACK", own(mod.TRAVEL_PACKAGE), out)


def header(num_records: int) -> bytes:
    body = b"".join(sub(t, p) for t, p in [
        (b"HEDR", struct.pack("<fiI", 1.71, num_records, mod.NEXT_OBJECT_ID)),
        (b"CNAM", zstring("skyrim_companion")),
        (b"SNAM", zstring("Гослинг — ИИ-компаньон (собрано tools/build_gosling_esp.py)")),
        (b"MAST", zstring("Skyrim.esm")),
        (b"DATA", struct.pack("<Q", 0)),
    ])
    return struct.pack("<4sIIIIHH", b"TES4", len(body), 0, 0, 0, FORM_VERSION, 0) + body


# ------------------------------------------------------------------ остальные файлы мода

def compile_scripts(data_dir: Path, out: Path) -> None:
    game = data_dir.parent
    compiler = game / "Papyrus Compiler" / "PapyrusCompiler.exe"
    vanilla = data_dir / "Source" / "Scripts"
    if not compiler.is_file() or not (vanilla / "TESV_Papyrus_Flags.flg").is_file():
        raise SystemExit(f"Нет компилятора Papyrus или исходников скриптов игры ({compiler}, {vanilla}). "
                         "Их ставит Creation Kit — запусти его один раз, он распакует Scripts.zip.")
    out.mkdir(parents=True, exist_ok=True)
    for psc in SCRIPTS_SRC.glob("*.psc"):
        result = subprocess.run(
            [str(compiler), str(psc), f"-f={vanilla / 'TESV_Papyrus_Flags.flg'}",
             f"-i={vanilla};{SCRIPTS_SRC}", f"-o={out}"],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f"Papyrus: ошибка компиляции {psc.name}:\n{result.stdout}\n{result.stderr}")
        print(f"Скрипт: {psc.stem}.pex")


def copy_facegen(data_dir: Path, out: Path) -> None:
    """Лицо Эрика-наёмника под номером Гослинга: без него у NPC без шаблона в игре тёмное лицо."""
    name = f"{TEMPLATE_NPC:08x}"
    for bsa, folder, target, ext in FACEGEN:
        with open(data_dir / bsa, "rb") as f:
            entries, flags = read_bsa_index(f)
            entry = next((e for e in entries if e.folder == folder and e.name == f"{name}.{ext}"), None)
            if entry is None:
                raise SystemExit(f"Нет {folder}\\{name}.{ext} в {bsa}")
            data = read_file(f, entry, flags)
        path = out / target / f"{mod.NPC:08X}.{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"Лицо: {path.relative_to(out)} ({len(data)} байт)")


def seq_file(out: Path) -> None:
    """Seq\\Gosling.seq — номера квестов «стартует с игрой», у которых есть диалоги (так делает CK/xEdit).
    Без него игра числит GoslingVQ запущенным, но его реплики не звучат: Say() молча ничего не делает."""
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{Path(mod.PLUGIN).stem}.seq").write_bytes(u32(own(mod.VOICE_QUEST)))


def placeholder_npc_voices(voice_root: Path, data_dir: Path) -> None:
    """Заглушки реплик NPC (разговоры с Гослингом) — в папке каждого типа голоса игры: сервис перед репликой пишет
    звук в папку типа голоса того, кто говорит. Тишина и .lip — копии заглушки Гослинга."""
    gosling = voice_root / mod.VOICE_TYPE
    wav = gosling / mod.voice_file(0)
    lip = wav.with_suffix(".lip")
    types = voice_types(data_dir)
    for edid in sorted(set(types.values()), key=str.lower):
        folder = voice_root / edid
        folder.mkdir(parents=True, exist_ok=True)
        for line in range(len(mod.NPC_LINES)):
            target = folder / mod.voice_file(line, mod.NPC_LINES)
            shutil.copyfile(wav, target)
            if lip.is_file():
                shutil.copyfile(lip, target.with_suffix(".lip"))
    print(f"Заглушки реплик NPC: {len(set(types.values()))} типов голоса")


def placeholder_voices(out: Path, lip_generator: Path) -> None:
    """Заглушки .wav и .lip: файлы должны существовать с запуска игры (MO2 строит виртуальную папку Data
    при старте), а сервис потом перезаписывает их содержимое перед каждой фразой. Секунда тишины:
    короче генератор .lip не разбирает."""
    out.mkdir(parents=True, exist_ok=True)
    for line in range(len(mod.LINES)):
        wav = out / mod.voice_file(line)
        sf.write(wav, np.zeros(44100, np.int16), 44100, subtype="PCM_16")
        if not lip_generator.is_file():
            print(f"Нет {lip_generator} — без заглушек .lip губы у Гослинга шевелиться не будут")
            continue
        # Генератор пишет промежуточный tmp16khz.wav в текущую папку — запускаем его во временной.
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([str(lip_generator), str(wav), "", "-Automated"], cwd=tmp,
                                    capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f"LipGenerator: не вышло сделать .lip для {wav.name}:\n{result.stdout}{result.stderr}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install", action="store_true", help="скопировать в MO2 (npc_voice.mod_dir)")
    args = ap.parse_args()

    cfg = load_config()
    esm = cfg.game.data_dir / "Skyrim.esm"
    print(f"Читаю {esm} ...")
    recs = find_records(esm, {TEMPLATE_NPC, INN_CELL, ERIK_IN_INN, TRAVEL_SAMPLE, LEAD_SAMPLE, FOLLOW_SAMPLE,
                                LINGER_SAMPLE})
    strings = load_strings(cfg.game.interface_bsa, "strings").get("skyrim", {})

    w = Writer()
    # Порядок групп — как в Skyrim.esm: NPC_, CELL, DIAL, QUST, PACK, VTYP, RELA.
    body = (w.group(b"NPC_", 0, gosling_npc(w, recs[TEMPLATE_NPC]))
            + inn_with_gosling(w, recs[INN_CELL], recs[ERIK_IN_INN], strings)
            + voice_topics(w)
            + w.group(b"QUST", 0, voice_quest(w) + travel_quest(w))
            + w.group(b"PACK", 0, travel_package(w, recs[TRAVEL_SAMPLE]) + lead_package(w, recs[LEAD_SAMPLE])
                      + approach_package(w, recs[FOLLOW_SAMPLE]) + linger_package(w, recs[LINGER_SAMPLE]))
            + voice_type(w)
            + w.group(b"RELA", 0, relationship(w)))

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    (OUT / mod.PLUGIN).write_bytes(header(w.count) + body)
    print(f"Плагин: {mod.PLUGIN} ({w.count} записей и групп)")
    compile_scripts(cfg.game.data_dir, OUT / "Scripts")
    copy_facegen(cfg.game.data_dir, OUT)
    seq_file(OUT / "Seq")
    placeholder_voices(OUT / "Sound" / "Voice" / mod.PLUGIN / mod.VOICE_TYPE, cfg.game.lip_generator)
    placeholder_npc_voices(OUT / "Sound" / "Voice" / mod.PLUGIN, cfg.game.data_dir)
    print(f"Готово: {OUT}")

    if args.install:
        for old in ("Sound", "meshes", "textures", "Seq"):  # прежние версии файлов мода — убрать
            if (cfg.npc_voice.mod_dir / old).exists():
                shutil.rmtree(cfg.npc_voice.mod_dir / old)
        shutil.copytree(OUT, cfg.npc_voice.mod_dir, dirs_exist_ok=True)
        print(f"Скопировано в {cfg.npc_voice.mod_dir}. В MO2: F5, включить мод «Gosling» и Gosling.esp на вкладке Plugins.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
