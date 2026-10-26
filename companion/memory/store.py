"""Память Гослинга: файл SQLite на каждого персонажа игрока (memory/<компаньон>/<ID персонажа>.db).

- journal — всё взаимодействие как было (реплики игрока и Гослинга, события игры, что слышал от NPC) с местом
  и игровой датой. Из него после перезапуска восстанавливается история разговора LLM.
- diary — дневник Гослинга: по записи на «главу» (пишет companion/memory/diarist.py по журналу).
- facts — что Гослинг узнал о мире: о месте, человеке, фракции, с источником («слышал от Хеймскра»).
- goals — цели («идём в Маркарт»), договорённости и что он знает о Владосе; активные — всегда в промпте.
  Там же мысли Гослинга (kind idea, пишет companion/memory/thinker.py): что держать в голове и предложить.
- encounters — кого и что встречали (раса врага, применённый Крик): первая встреча — удивление, следующие —
  «уже встречали N раз, последний — там-то».
- soul — душа Гослинга: что он решил о себе (оружие, еда, страхи, мечты, мнения). Журнал изменений: нынешнее —
  последняя запись по каждому ключу, пустой текст — передумал без замены (пишет companion/memory/soul.py).
Подробно — production_logs/2026-10-14_память.md.

Привязка к прохождению: каждая запись хранит игровую дату (дней прошло). Загрузили сейв старше —
записи «из будущего» уходят в архив (archived = 1): в этой ветке их не было. Во всех таблицах.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS journal (
    id        INTEGER PRIMARY KEY,
    real_time REAL NOT NULL,      -- time.time()
    game_days REAL,               -- игровая дата: дней прошло (NULL — неизвестна)
    kind      TEXT NOT NULL,      -- player | gosling | event | heard
    text      TEXT NOT NULL,
    emotion   TEXT,               -- у реплик Гослинга
    initiative INTEGER DEFAULT 0, -- Гослинг заговорил сам
    place     TEXT,               -- где были (локация, помещение)
    archived  INTEGER DEFAULT 0   -- из «будущего» загруженного сейва
);
CREATE INDEX IF NOT EXISTS journal_live ON journal (archived, id);
CREATE TABLE IF NOT EXISTS diary (
    id        INTEGER PRIMARY KEY,
    real_time REAL NOT NULL,
    game_days REAL,               -- игровая дата конца главы
    place     TEXT,               -- где прошла глава
    text      TEXT NOT NULL,      -- запись от лица Гослинга
    journal_to INTEGER NOT NULL,  -- последняя запись журнала, которую она покрывает
    archived  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS facts (
    id        INTEGER PRIMARY KEY,
    real_time REAL NOT NULL,
    game_days REAL,
    subject   TEXT NOT NULL,      -- о ком/о чём: место, человек, фракция, задание
    text      TEXT NOT NULL,
    source    TEXT,               -- откуда знает: «был сам», «слышал от Хеймскра», «Владос сказал»
    archived  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS goals (
    id        INTEGER PRIMARY KEY,
    real_time REAL NOT NULL,
    game_days REAL,
    kind      TEXT NOT NULL,      -- goal (цель) | promise (договорённость) | player (о Владосе) | idea (мысль)
    text      TEXT NOT NULL,
    status    TEXT DEFAULT 'active',  -- active | done | dropped | said (мысль высказана вслух)
    archived  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS encounters (
    id        INTEGER PRIMARY KEY,
    real_time REAL NOT NULL,
    game_days REAL,
    category  TEXT NOT NULL,      -- раса | крик
    key       TEXT NOT NULL,      -- «дракон», «безжалостная сила» (в нижнем регистре)
    place     TEXT,
    archived  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS encounters_key ON encounters (category, key, archived);
CREATE TABLE IF NOT EXISTS soul (
    id        INTEGER PRIMARY KEY,
    real_time REAL NOT NULL,
    game_days REAL,
    key       TEXT NOT NULL,      -- о чём: «оружие», «еда», «страх: пауки» (в нижнем регистре)
    text      TEXT NOT NULL,      -- что решил о себе; пусто — передумал и не заменил
    why       TEXT,               -- откуда: что сказал и в ответ на что
    feel      TEXT,               -- поводы для настроения (JSON): {"страх": ["паук"], "радость": ["топор"]}
    archived  INTEGER DEFAULT 0
);
"""
TABLES = ("journal", "diary", "facts", "goals", "encounters", "soul")
# Журнал старше таблицы встреч: один раз собираем из него, с кем уже бились (по именам — у обычных существ
# имя совпадает с расой: «Драугр», «Злокрыс»), и драконов — по поглощённым душам.
BACKFILL_FIGHT = re.compile(r"^(?:начался бой|игрок убил|убит): (.+)$")
BACKFILL_DRAGON = "игрок поглотил душу дракона"
GOAL_KINDS = ("goal", "promise", "player")
FUTURE_EPSILON = 0.001  # дней (~1,5 мин игрового времени): автосейв «тот же момент» — не будущее


@dataclass(frozen=True)
class Where:
    """Где и когда в игре — для каждой записи."""
    place: str = ""
    game_days: float | None = None


def memory_key(character: str, player_name: str) -> str:
    """Имя файла памяти: ID персонажа из сейва, иначе имя игрока."""
    key = character.strip() or player_name.strip()
    return re.sub(r"[^\w-]+", "_", key) or "unknown"


class MemoryStore:
    """Потокобезопасно: пишут обработчик событий игры, голосовой сервис и мозг."""

    def __init__(self, root: Path):
        self.root = root
        self.key: str | None = None
        self.player_name = ""
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._db is not None

    def open(self, character: str, player_name: str, game_days: float | None) -> bool:
        """Память этого персонажа. True — память поменялась (другой персонаж, первый раз или часть записей
        ушла в архив как «будущее») — историю разговора пора собрать заново."""
        key = memory_key(character, player_name)
        with self._lock:
            switched = key != self.key
            if switched:
                if self._db:
                    self._db.close()
                self.root.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(self.root / f"{key}.db", check_same_thread=False)
                self._db.executescript(SCHEMA)
                if "feel" not in {r[1] for r in self._db.execute("PRAGMA table_info(soul)")}:  # база до 2026-10-19
                    self._db.execute("ALTER TABLE soul ADD COLUMN feel TEXT")
                self.key = key
                log.info("Память: %s (%s), записей в журнале: %d", key, player_name or "?",
                         self._db.execute("SELECT count(*) FROM journal WHERE archived = 0").fetchone()[0])
                self._backfill_encounters()
            self.player_name = player_name or self.player_name
            if player_name:
                self._db.execute("INSERT OR REPLACE INTO meta VALUES ('player_name', ?)", (player_name,))
            archived = 0
            if game_days is not None:
                archived = sum(self._db.execute(
                    f"UPDATE {table} SET archived = 1 WHERE archived = 0 AND game_days > ?",
                    (game_days + FUTURE_EPSILON,)).rowcount for table in TABLES)
                if archived:
                    log.info("Память: загружен сейв постарше — %d записей «из будущего» в архив", archived)
            self._db.commit()
            return switched or archived > 0

    def record(self, kind: str, text: str, where: Where, emotion: str | None = None,
               initiative: bool = False) -> None:
        if not text:
            return
        with self._lock:
            if not self._db:
                return  # персонаж ещё неизвестен (игра не загружена) — помнить некому
            self._db.execute(
                "INSERT INTO journal (real_time, game_days, kind, text, emotion, initiative, place) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), where.game_days, kind, text, emotion, int(initiative), where.place or None))
            self._db.commit()

    def visits(self, own: bool = False) -> list[tuple[str, float, float]]:
        """Где бывали вместе: (место, первая и последняя игровая дата), свежие первыми. Место — локация
        (первая часть «Вайтран, Гарцующая кобыла»); дикие земли не считаем. own — где Гослинг был сам, без Владоса
        (записи вида own): без этого места одиночной вылазки попадали в «где вы бывали вместе» (21.10)."""
        with self._lock:
            if not self._db:
                return []
            rows = self._db.execute(
                "SELECT place, min(game_days), max(game_days) FROM journal "
                "WHERE archived = 0 AND place IS NOT NULL AND game_days IS NOT NULL AND (kind = 'own') = ? "
                "GROUP BY place", (own,)).fetchall()
        merged: dict[str, tuple[float, float]] = {}
        for place, first, last in rows:
            name = place.split(", ")[0]
            if name == "дикие земли":
                continue
            was = merged.get(name)
            merged[name] = (min(first, was[0]), max(last, was[1])) if was else (first, last)
        return sorted(((n, f, l) for n, (f, l) in merged.items()), key=lambda v: -v[2])

    def get_meta(self, key: str) -> str | None:
        rows = self._rows("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0][0] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._write("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def recent(self, kind: str, seconds: float) -> list[tuple[float, str]]:
        """Записи вида kind за последние seconds секунд: (real_time, текст)."""
        with self._lock:
            if not self._db:
                return []
            return self._db.execute(
                "SELECT real_time, text FROM journal WHERE archived = 0 AND kind = ? AND real_time > ? ORDER BY id",
                (kind, time.time() - seconds)).fetchall()

    def last_dialogue(self, turns: int) -> list[tuple[str, str, str | None, bool]]:
        """Последние реплики игрока и Гослинга по порядку, а между ними — события игры:
        (kind, text, emotion, initiative), kind — player | gosling | event."""
        with self._lock:
            if not self._db:
                return []
            rows = self._db.execute(
                "SELECT id, kind, text, emotion, initiative FROM journal "
                "WHERE archived = 0 AND kind IN ('player', 'gosling') ORDER BY id DESC LIMIT ?",
                (turns * 2,)).fetchall()
            if rows:
                rows += self._db.execute(
                    "SELECT id, kind, text, emotion, initiative FROM journal "
                    "WHERE archived = 0 AND kind = 'event' AND id > ?", (rows[-1][0],)).fetchall()
        return [(kind, text, emotion, bool(initiative)) for _id, kind, text, emotion, initiative in sorted(rows)]

    # ------------------------------------------------------------ дневник, факты, цели

    def _rows(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall() if self._db else []

    def _write(self, sql: str, args: tuple) -> int | None:
        with self._lock:
            if not self._db:
                return None
            row_id = self._db.execute(sql, args).lastrowid
            self._db.commit()
            return row_id

    def journal_since(self, after_id: int) -> list[tuple[int, float | None, str, str, str | None]]:
        """Журнал после записи after_id: (id, игровая дата, вид, текст, место)."""
        return self._rows("SELECT id, game_days, kind, text, place FROM journal "
                          "WHERE archived = 0 AND id > ? ORDER BY id", (after_id,))

    def diary_last_covered(self) -> tuple[int, float]:
        """(последняя запись журнала, покрытая дневником; когда написана последняя запись дневника)."""
        rows = self._rows("SELECT max(journal_to), max(real_time) FROM diary WHERE archived = 0")
        return (rows[0][0] or 0, rows[0][1] or 0.0) if rows else (0, 0.0)

    def add_diary(self, text: str, where: Where, journal_to: int) -> None:
        self._write("INSERT INTO diary (real_time, game_days, place, text, journal_to) VALUES (?, ?, ?, ?, ?)",
                    (time.time(), where.game_days, where.place or None, text, journal_to))

    def recent_diary(self, count: int) -> list[tuple[float | None, str | None, str]]:
        """Последние записи дневника по порядку: (игровая дата, место, текст)."""
        rows = self._rows("SELECT game_days, place, text FROM diary WHERE archived = 0 ORDER BY id DESC LIMIT ?",
                          (count,))
        return list(reversed(rows))

    def add_fact(self, subject: str, text: str, source: str, where: Where) -> None:
        if not text.strip():
            return
        self._write("INSERT INTO facts (real_time, game_days, subject, text, source) VALUES (?, ?, ?, ?, ?)",
                    (time.time(), where.game_days, subject.strip() or "разное", text.strip(), source.strip() or None))

    def facts(self) -> list[tuple[str, str, str | None]]:
        """Все известные факты: (о чём, факт, откуда), старые первыми."""
        return self._rows("SELECT subject, text, source FROM facts WHERE archived = 0 ORDER BY id")

    def add_goal(self, kind: str, text: str, where: Where) -> None:
        if kind in GOAL_KINDS and text.strip():
            self._write("INSERT INTO goals (real_time, game_days, kind, text) VALUES (?, ?, ?, ?)",
                        (time.time(), where.game_days, kind, text.strip()))

    def active_goals(self) -> list[tuple[int, str, str]]:
        """(id, вид, текст) активных целей, договорённостей и знаний о Владосе (без мыслей)."""
        return self._rows("SELECT id, kind, text FROM goals WHERE archived = 0 AND status = 'active' "
                          "AND kind != 'idea' ORDER BY id")

    def ideas(self) -> list[str]:
        """Текущие мысли Гослинга, по порядку."""
        return [text for (text,) in self._rows(
            "SELECT text FROM goals WHERE archived = 0 AND status = 'active' AND kind = 'idea' ORDER BY id")]

    def replace_ideas(self, ideas: list[str], where: Where, area: str = "") -> None:
        """Новый набор мыслей вместо прежнего (прежние — dropped: в архиве, видно, что он думал).
        area — где надумал (поселение или «дикие земли»): ушли оттуда — мысли не к месту, пока не подумает заново."""
        with self._lock:
            if not self._db:
                return
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('ideas_area', ?)", (area,))
            self._db.execute("UPDATE goals SET status = 'dropped' WHERE kind = 'idea' AND status = 'active'")
            self._db.executemany("INSERT INTO goals (real_time, game_days, kind, text) VALUES (?, ?, 'idea', ?)",
                                 [(time.time(), where.game_days, idea) for idea in ideas])
            self._db.commit()

    def close_goal(self, goal_id: int, status: str) -> None:
        if status in ("done", "dropped"):
            self._write("UPDATE goals SET status = ? WHERE id = ?", (status, goal_id))

    def mark_idea_said(self, text: str) -> None:
        """Мысль высказана вслух: из «Твоих мыслей» — вон (иначе модель твердит её снова: «зовёшь в конюшню»),
        мыслитель видит её в «уже сказал»."""
        self._write("UPDATE goals SET status = 'said' WHERE kind = 'idea' AND status = 'active' AND text = ?", (text,))

    def said_ideas(self, count: int, within: float = 0) -> list[tuple[str, float]]:
        """Высказанные мысли с временем (реальным), свежие первыми; within — только за столько секунд (0 — любые)."""
        return self._rows(
            "SELECT text, real_time FROM goals WHERE archived = 0 AND kind = 'idea' AND status = 'said' "
            "AND real_time >= ? ORDER BY id DESC LIMIT ?", (time.time() - within if within else 0, count))

    # ------------------------------------------------------------ вспоминание (справка recall)

    def all_diary(self) -> list[tuple[float | None, str | None, str]]:
        """Весь дневник: (игровая дата, место, текст), старые первыми."""
        return self._rows("SELECT game_days, place, text FROM diary WHERE archived = 0 ORDER BY id")

    def all_goals(self) -> list[tuple[float | None, str, str, str]]:
        """Цели, договорённости, что знает о Владосе — и закрытые: (игровая дата, вид, текст, статус)."""
        return self._rows("SELECT game_days, kind, text, status FROM goals WHERE archived = 0 AND kind != 'idea' "
                          "ORDER BY id")

    def journal_all(self) -> list[tuple[float | None, str, str, str | None]]:
        """Весь журнал этой ветки: (игровая дата, вид, текст, место), старые первыми."""
        return self._rows("SELECT game_days, kind, text, place FROM journal WHERE archived = 0 ORDER BY id")

    def facts_with_ids(self) -> list[tuple[int, str, str, str | None]]:
        """(id, о чём, факт, откуда) — для сверки с данными игры."""
        return self._rows("SELECT id, subject, text, source FROM facts WHERE archived = 0 ORDER BY id")

    def retire_fact(self, fact_id: int) -> None:
        """Факт не сходится с данными игры — убрать (archived = 2: не «будущее», а неверное; не стирается)."""
        self._write("UPDATE facts SET archived = 2 WHERE id = ?", (fact_id,))

    # ------------------------------------------------------------ душа

    def soul(self) -> list[tuple[str, str]]:
        """Нынешняя душа: (ключ, текст) — последняя запись по каждому ключу этой ветки сейва, без пустых.
        Журнал изменений, а не «заменить строку»: загрузили сейв постарше — поздние записи в архиве, и душа сама
        такая, какой была тогда."""
        return [(key, text) for key, text in self._rows(
            "SELECT key, text FROM soul s WHERE archived = 0 AND id = "
            "(SELECT MAX(id) FROM soul WHERE archived = 0 AND key = s.key) ORDER BY id")
            if text]

    def soul_feel(self) -> list[tuple[str, str, dict | None]]:
        """Нынешняя душа с поводами для настроения: (ключ, текст, {чувство: [слова]}); None — поводов ещё не
        спрашивали (запись до 2026-10-19), {} — спрашивали, их нет."""
        out = []
        for key, text, feel in self._rows(
                "SELECT key, text, feel FROM soul s WHERE archived = 0 AND id = "
                "(SELECT MAX(id) FROM soul WHERE archived = 0 AND key = s.key) ORDER BY id"):
            if text:
                try:
                    out.append((key, text, json.loads(feel) if feel is not None else None))
                except json.JSONDecodeError:
                    out.append((key, text, None))
        return out

    def set_soul(self, key: str, text: str, why: str, where: Where, feel: dict | None = None) -> None:
        self._write("INSERT INTO soul (real_time, game_days, key, text, why, feel) VALUES (?, ?, ?, ?, ?, ?)",
                    (time.time(), where.game_days, key.strip().lower(), text.strip(), why.strip() or None,
                     json.dumps(feel, ensure_ascii=False) if feel is not None else None))

    # ------------------------------------------------------------ встречи

    def last_encounters(self, category: str, key: str) -> list[tuple[float, float | None, str | None]]:
        """Прошлые встречи (real_time, игровая дата, место), свежие первыми."""
        return self._rows("SELECT real_time, game_days, place FROM encounters "
                          "WHERE archived = 0 AND category = ? AND key = ? ORDER BY id DESC",
                          (category, key.lower()))

    def deaths(self) -> list[str]:
        """Гибели Владоса в этой ветке сейва — тексты событий «Владос погиб (его убил …)» (события died пишутся уже
        после загрузки, поэтому переживают архив «будущего»)."""
        return [text for (text,) in self._rows(
            "SELECT text FROM journal WHERE archived = 0 AND kind = 'event' AND text LIKE 'Владос погиб (%'")]

    def last_by_key(self, category: str) -> dict[str, tuple[float, float | None]]:
        """Последняя встреча каждого ключа категории: key -> (real_time, игровая дата). Для шуток: что когда сказано."""
        return {key: (real, days) for key, real, days in self._rows(
            "SELECT key, MAX(real_time), game_days FROM encounters WHERE archived = 0 AND category = ? GROUP BY key",
            (category,))}

    def add_encounter(self, category: str, key: str, where: Where) -> None:
        self._write("INSERT INTO encounters (real_time, game_days, category, key, place) VALUES (?, ?, ?, ?, ?)",
                    (time.time(), where.game_days, category, key.lower(), where.place or None))

    def rename_encounters(self, category: str, legacy: dict[str, str]) -> None:
        """Встречи под прежними ключами — под нынешними (раса «dragon» стала «дракон»): счётчик «уже встречали»
        не теряется."""
        with self._lock:
            if not self._db:
                return
            for old, new in legacy.items():
                self._db.execute("UPDATE encounters SET key = ? WHERE category = ? AND key = ?", (new, category, old))
            self._db.commit()

    def _backfill_encounters(self) -> None:
        """Под блокировкой open(): один раз на базу — встречи из старого журнала."""
        if self._db.execute("SELECT 1 FROM meta WHERE key = 'encounters_backfilled'").fetchone():
            return
        seen: set[str] = set()
        rows = []
        for real_time, days, text, place in self._db.execute(
                "SELECT real_time, game_days, text, place FROM journal WHERE archived = 0 AND kind = 'event' ORDER BY id"):
            names = ["дракон"] if text == BACKFILL_DRAGON else (
                m[1].split(", ") if (m := BACKFILL_FIGHT.match(text)) else [])
            for name in names:
                key = re.sub(r" ×\d+$", "", name).lower()
                if key and key not in seen:
                    seen.add(key)
                    rows.append((real_time, days, "раса", key, place))
        self._db.executemany("INSERT INTO encounters (real_time, game_days, category, key, place) "
                             "VALUES (?, ?, ?, ?, ?)", rows)
        self._db.execute("INSERT OR REPLACE INTO meta VALUES ('encounters_backfilled', '1')")
        log.info("Память: встречи из журнала — %d", len(rows))
