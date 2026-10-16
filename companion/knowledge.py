"""Знания Гослинга о мире — из файлов игры, а не из памяти модели (production_logs/2026-10-15_план_памяти.md).

Модели уверенно путают лор (Лукан «в лавке Белетора», «Ту'ум — город»), а летописец потом закрепляет выдумки.
Поэтому что знает Гослинг-местный — собираем из данных игры и ищем по ним (справка world_info):
- **общеизвестное** — экраны загрузки (LSCR): «Мара — богиня любви и сострадания. В Рифтене есть посвящённый ей
  храм». Подсказки по игре («силовая атака пробивает блок») отсеиваем по EditorID;
- **люди** — кто где живёт и кем работает (из справки о местах, companion/game/places.py): «Лукан Валерий —
  торговец; Галантерея Лукана, Ривервуд»;
- **места** — что это и в каком владении: «Ветреный пик — склеп драугров, нордские руины; владение Вайтран».

Поиск — SQLite FTS5 с триграммами (русский без стеммера): слово запроса режем до основы («Лукана» -> «Лука»),
основы — через OR, лучшие по bm25 (имя/название весит больше текста). Строится в памяти при первом обращении
(~1 с), файлы игры не меняются.

Дальше по плану: обновление по ходу игры («были, зачистили»), прочитанные книги, слухи из реплик NPC.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import struct
import threading
from pathlib import Path

from companion.game.books import BookLibrary, plugin_records
from companion.game.dialogue import PLUGINS
from companion.game.gamefiles import load_strings
from companion.game.places import JOBS, SETTLEMENT, PlaceBook

log = logging.getLogger(__name__)

# Экраны загрузки — про игру, а не про мир (по началу EditorID): их не берём.
MECHANICS = ("Character", "Combat", "Magic", "Racial", "Items", "Crime", "Smithing", "Shops", "Alchemy", "Spells",
             "Power", "Level", "Enchanting", "Followers", "Shouts", "Journal", "Chargen", "Containers", "Dual",
             "Detection", "Equipment", "Bed", "Altar", "Spellscancel", "Fast", "Help", "Skill", "Lockpick",
             "Pickpocket", "Sneak", "Stealth", "Horse", "Werewolf", "Vampire", "Mount", "Dragonriding")
STOP = {"кто", "что", "где", "как", "такой", "такая", "такое", "такие", "это", "этот", "эта", "про", "расскажи",
        "знаешь", "слышал", "какой", "какая", "какие", "был", "была", "были", "есть", "там", "тут", "его", "её",
        "них", "для", "или", "что-то", "чем", "чего", "кого", "кому", "когда", "почему", "зачем", "нибудь",
        "куда", "откуда", "делись", "делся", "можно", "надо", "нужно", "сейчас", "вообще", "ли"}
LIMIT = 6
LATIN = re.compile(r"[A-Za-z]")
CANDIDATES = 80   # кандидатов из FTS — потом переранжируем
PER_KIND = 3      # записей одного вида в ответе — не больше
SOURCES = {"lore": "общеизвестное", "person": "люди", "place": "места"}
READERS = {"владос": "читал Владос", "гослинг": "читал сам"}  # кто прочитал книгу
CHUNK = 700      # знаков в куске книги для поиска
BOOK_KINDS = tuple(f"book:{who}" for who in READERS)


def stems(query: str) -> list[str]:
    """Основы слов запроса для триграммного поиска: «Лукана» -> «Лука», «Седобородых» -> «Седобород»."""
    out = []
    for w in re.findall(r"[\w'-]+", query.lower()):
        if len(w) < 3 or w in STOP:
            continue
        out.append(w if len(w) <= 4 else w[:max(4, len(w) - 2)])
    return list(dict.fromkeys(out))


def label(kind: str) -> str:
    """Подпись знания для модели: [общеизвестное], [книга, читал Владос]…"""
    if kind.startswith("book:"):
        return "книга, " + READERS.get(kind[5:], kind[5:])
    return SOURCES.get(kind, kind)


def chunks(text: str, size: int = CHUNK) -> list[str]:
    """Текст книги — кусками по абзацам, не длиннее size (длинный абзац — по предложениям)."""
    parts, cur = [], ""
    for para in [p for p in text.split("\n") if p.strip()]:
        pieces = [para] if len(para) <= size else re.split(r"(?<=[.!?…])\s+", para)
        for piece in pieces:
            if cur and len(cur) + len(piece) + 1 > size:
                parts.append(cur)
                cur = ""
            cur = f"{cur} {piece}".strip() if cur else piece
    if cur:
        parts.append(cur)
    return parts


class Knowledge:
    """Что Гослинг знает о мире как местный — поиск по данным игры. Потокобезопасно."""

    def __init__(self, data_dir: Path, places: PlaceBook | None = None, books: BookLibrary | None = None):
        self.data_dir = data_dir
        self.places = places
        self.books = books
        self._read: dict[str, str] = {}  # прочитанные в этой ветке сейва: ключ книги -> кто (владос | гослинг)
        self._chunk_pos: dict[tuple[str, str], int] = {}  # (название, кусок) -> номер куска в книге
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._titles: list[tuple[str, tuple[str, str, str]]] = []  # (название в нижнем регистре, запись)

    # ------------------------------------------------------------ поиск

    def search(self, query: str, limit: int = LIMIT) -> list[tuple[str, str]]:
        """[(вид, текст)] — лучшие совпадения; вид — lore | person | place.

        Триграммы находят и внутри слов («Мара» -> «Грогмара», «делись» -> «изделие»), поэтому кандидатов
        переранжируем: основа с начала слова в названии — 3, в тексте — 1; каждая совпавшая основа запроса — ещё 2
        («кто ярл Вайтрана»: Балгруф — и «ярл», и «Вайтран» — выше «Вайтран - Конюшни»); название — слово из
        запроса («Рифтене» -> «Рифтен») — ещё 5, и такие ищем отдельно: склады и тюрьмы Рифтена иначе вытесняют
        сам город из кандидатов. Не больше PER_KIND одного вида — на «Талмор» не десять жителей посольства."""
        words = stems(query)
        if not words:
            return []
        match = " OR ".join('"' + w.replace('"', "") + '"' for w in words)
        db = self._load()
        with self._lock:
            try:
                rows = db.execute("SELECT kind, title, text FROM know WHERE know MATCH ? "
                                  "ORDER BY bm25(know, 0, 6.0, 1.0) LIMIT ?", (match, CANDIDATES)).fetchall()
            except sqlite3.OperationalError as e:  # кривой запрос — просто ничего не нашли
                log.warning("Знания: не найти «%s» (%s)", query, e)
                return []
        named = [re.compile(re.escape(w) + r"\w{0,3}") for w in words]
        rows = [r for title, r in self._titles if any(n.fullmatch(title) for n in named)] + rows
        starts = [re.compile(r"(?<!\w)" + re.escape(w)) for w in words]
        scored, seen = [], set()
        for order, (kind, title, text) in enumerate(rows):
            if (kind, title, text) in seen:
                continue
            seen.add((kind, title, text))
            t, x = title.casefold(), text.casefold()
            hits = [(bool(s.search(t)), bool(s.search(x))) for s in starts]
            score = (sum(3 * a + b for a, b in hits) + 2 * sum(a or b for a, b in hits)
                     + 5 * any(n.fullmatch(t) for n in named)
                     + (kind == "lore" or kind in BOOK_KINDS))  # при прочих равных — объяснение и прочитанное,
            #                                                       а не перечень руин
            if any(a or b for a, b in hits):
                # Куски одной книги при равных очках — по порядку текста: «о чём книга?» — с начала, а не с середины.
                pos = self._chunk_pos.get((title, text)) if kind in BOOK_KINDS else None
                scored.append((-score, 0 if pos is not None else 1, pos if pos is not None else order, kind, title, text))
        scored.sort()
        out, per_kind = [], {}
        for _s, _b, _o, kind, title, text in scored:
            if per_kind.get(kind, 0) < PER_KIND:
                per_kind[kind] = per_kind.get(kind, 0) + 1
                out.append((kind, f"{title} — {text}" if title else text))
            if len(out) >= limit:
                break
        return out

    def answer(self, query: str) -> str:
        """Текст справки world_info для модели."""
        found = self.search(query)
        if not found:
            return (f"О «{query}» ты ничего толком не знаешь. Так и скажи — не выдумывай; можно «не слышал» или "
                    "«надо бы спросить местных».")
        return ("Что ты знаешь (это верные знания о мире; чего тут нет — того ты не знаешь, не додумывай):\n"
                + "\n".join(f"- [{label(kind)}] {text}" for kind, text in found))

    # ------------------------------------------------------------ прочитанные книги

    def set_read(self, read: dict[str, str]) -> None:
        """Прочитанное в этой ветке сейва (память открыли заново): ключ книги -> кто прочитал. Книги в поиске —
        только эти: Гослинг знает книгу, лишь если её прочитал он или Владос."""
        db = self._load()
        with self._lock:
            # Не LIKE: в FTS5 с триграммами LIKE по колонке молча ничего не находит.
            db.execute("DELETE FROM know WHERE kind IN (?, ?)", BOOK_KINDS)
            self._read = {}
        for key, who in read.items():
            self.add_read(key, who)

    def add_read(self, key: str, who: str) -> bool:
        """Книгу прочитали — её куски в поиск. True — новая (раньше в этой ветке не читали)."""
        book = self.books.by_key(key) if self.books else None
        if not book or self._read.get(key) in (who, "гослинг"):  # сам читал — знает лучше, чем «Владос читал»
            return False
        db = self._load()
        with self._lock:
            db.execute("DELETE FROM know WHERE kind IN (?, ?) AND title = ?", (*BOOK_KINDS, f"«{book.title}»"))
            parts = chunks(book.text)
            db.executemany("INSERT INTO know VALUES (?, ?, ?)", [(f"book:{who}", f"«{book.title}»", p) for p in parts])
            self._chunk_pos.update({(f"«{book.title}»", p): i for i, p in enumerate(parts)})
            db.commit()
            self._read[key] = who
        log.info("Знания: книга «%s» — %s", book.title, READERS.get(who, who))
        return True

    # ------------------------------------------------------------ сборка

    def _load(self) -> sqlite3.Connection:
        with self._lock:
            if self._db is None:
                db = sqlite3.connect(":memory:", check_same_thread=False)
                db.execute("CREATE VIRTUAL TABLE know USING fts5(kind UNINDEXED, title, text, tokenize='trigram')")
                rows: list[tuple[str, str, str]] = []
                try:
                    rows += self._lore()
                except OSError as e:
                    log.warning("Знания: не прочитать экраны загрузки (%s)", e)
                if self.places:
                    rows += self._people_and_places()
                db.executemany("INSERT INTO know VALUES (?, ?, ?)", rows)
                db.commit()
                self._db = db
                self._titles = [(r[1].casefold(), r) for r in rows if r[1]]
                log.info("Знания о мире: %d записей (%s)", len(rows),
                         ", ".join(f"{SOURCES[k]} {sum(r[0] == k for r in rows)}" for k in SOURCES))
            return self._db

    def _lore(self) -> list[tuple[str, str, str]]:
        """Экраны загрузки о мире (не подсказки по игре)."""
        strings = load_strings(self.data_dir / "Skyrim - Interface.bsa", "strings")
        out, seen = [], set()
        for plugin in PLUGINS:
            path = self.data_dir / f"{plugin}.esm"
            if not path.is_file():
                continue
            for _typ, _form, subs in plugin_records(path, {b"LSCR"}):
                edid = subs.get(b"EDID", [b""])[0].rstrip(b"\0").decode("cp1252", "replace")
                core = edid.removeprefix("x").removeprefix("DLC1").removeprefix("DLC2")
                if not subs.get(b"DESC") or core.startswith(MECHANICS):
                    continue
                sid = struct.unpack_from("<I", subs[b"DESC"][0])[0]
                text = (strings.get(plugin, {}).get(sid) or strings.get("skyrim", {}).get(sid) or "").strip()
                if text and text not in seen:
                    seen.add(text)
                    out.append(("lore", "", text))
        return out

    def _people_and_places(self) -> list[tuple[str, str, str]]:
        """Люди (кто где живёт и кем работает) и места (что это, в каком владении)."""
        places, _ = self.places._index()
        people: dict[str, dict] = {}
        out = []
        for place in places.values():
            if LATIN.search(place.name):  # непереведённые служебные локации: «Location for things…», «E3 …»
                continue
            town = self.places.settlement_of(place)
            hold = self.places.hold_of(place)
            around = []  # где это: поселение, владение
            if town and town.key != place.key:
                around.append(town.name)
            if hold and hold.key != place.key and (not town or hold.name.removeprefix("Владение ") != town.name):
                around.append(f"владение {hold.name.removeprefix('Владение ')}")
            kinds = ", ".join(place.kinds())
            where = place.name + (f" ({kinds})" if kinds else "") + "".join(f", {a}" for a in around)
            # Место: что это и где — для «что за Ветреный пик?»; поселения — карточкой целиком.
            if any(place.has(k) for k in SETTLEMENT):
                out.append(("place", place.name, self.places.card(place.name).split(" — ", 1)[-1]))
            elif kinds and not place.has("LocTypeHold"):
                out.append(("place", place.name, "; ".join([kinds, *around])))
            for resident in place.residents:
                job, name = _split_job(resident)
                p = people.setdefault(name, {"jobs": [], "where": []})
                if job and job not in p["jobs"]:
                    p["jobs"].append(job)
                if where not in p["where"]:
                    p["where"].append(where)
        for name, p in people.items():
            text = (", ".join(p["jobs"]) + "; " if p["jobs"] else "") + "где живёт: " + "; ".join(p["where"][:3])
            out.append(("person", name, text))
        return out


def _split_job(resident: str) -> tuple[str, str]:
    """«торговец Лукан Валерий (учит красноречию)» -> («торговец, учит красноречию», «Лукан Валерий»)."""
    teaches = ""
    if m := re.search(r" \((учит [^)]*)\)$", resident):
        teaches, resident = m[1], resident[:m.start()]
    job = next((j for j in sorted(JOBS.values(), key=len, reverse=True) if resident.startswith(j + " ")), "")
    name = resident[len(job) + 1:] if job else resident
    return ", ".join(x for x in (job, teaches) if x), name
