"""Летописец: по журналу памяти пишет дневник Гослинга, факты и цели — фоновым запросом к модели.

Глава кончилась, когда:
- ушли из места (другое поселение или дикие земли) и с прошлой записи накопилось хоть что-то (diarist.min_entries);
- набралось много записей (diarist.big_dialogue реплик или diarist.big_total записей всего);
- прошло diarist.chapter_seconds с первой неописанной записи (и их не меньше min_entries).
Настройки — секция diarist в config.yaml (DiaristConfig).

Модель получает записи главы, уже известные факты и активные цели; возвращает JSON: запись в дневник (2–4
предложения от лица Гослинга), новые факты (о чём, что, откуда знает), новые цели/договорённости/знания
о Владосе и какие цели выполнены или отменены. Разговор при этом не ждёт — запрос идёт в своём потоке.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from companion.config import DiaristConfig
from companion.memory.store import GOAL_KINDS, MemoryStore, Where

log = logging.getLogger(__name__)


SYSTEM = """Ты — Гослинг, спутник Владоса в Скайриме: весёлый молодой норд, бывший картограф из Солитьюда.
Ты ведёшь дневник и записываешь, что важно запомнить. Пишешь по-русски.
Отвечай строго одним JSON-объектом без пояснений и markdown:
{"diary": "...", "facts": [{"subject": "...", "text": "...", "source": "..."}],
 "goals_new": [{"kind": "goal|promise|player", "text": "..."}], "goals_done": [id, ...], "goals_dropped": [id, ...]}

diary — запись в дневник от первого лица, 2–4 предложения: что было главного и что ты об этом думаешь, в своём
характере. Только то, что есть в записях, — ничего не выдумывай. Ничего примечательного — одно короткое предложение.
facts — только новое, конкретное и полезное потом: где что находится, кто что рассказал о месте, человеке,
задании, слухи, важные поступки Владоса. Не повторяй то, что уже знаешь. subject — название места, имя человека,
фракции или задания; source — «был сам», «слышал от <имя>» или «Владос сказал». Обычно 0–3 факта.
Твои собственные реплики («Ты (Гослинг)») — не источник фактов: ты мог ошибиться или пошутить. Факт берётся только
из событий, слов NPC и слов Владоса. Что вы куда-то пришли — не факт (где вы бывали, память знает сама);
факт — что там есть, кто там и что там случилось.
goals_new — только явно прозвучавшее: goal — куда идём или что собираемся сделать (по заданию или раз Владос
сказал или согласился; твоё предложение, на которое он не ответил «да», — не цель); promise — о чём условились;
player — что узнал о самом Владосе (что любит, чего не любит, привычки). goals_done / goals_dropped — id
выполненных или отменённых целей из списка ниже."""


def game_time(days: float | None) -> str:
    if days is None:
        return "?"
    hours = days % 1 * 24
    return f"день {int(days) + 1}, {int(hours):02d}:{int(hours % 1 * 60):02d}"


VERIFY_SYSTEM = """Ты сверяешь заметки Гослинга о Скайриме с данными игры. Данные игры всегда верны, но неполны.
Заметка неверна, только если данные прямо говорят иное о том же самом. Пример: заметка «Лукан работает в «Товарах
Белетора» в Вайтране», данные «Лукан Валерий — торговец; Галантерея Лукана, Ривервуд» — неверна.
Не противоречие (заметка верна): человека, события или слуха в данных нет; данные молчат о направлении, дороге,
расстоянии; в заметке не сказано, о чём говорят данные (например, владение); это чьи-то слова или слухи — данные о
них молчат; формулировка другая, а суть та же. Сомневаешься — заметка верна.
Отвечай строго одним JSON-объектом без пояснений и markdown:
{"wrong": [номера неверных], "why": {"номер": "что именно в данных говорит иное, коротко"}}"""
VERIFY_EVIDENCE = 4      # записей базы знаний на заметку
VERIFY_CHARS = 500       # знаков записи базы знаний


def verify_facts(facts: list[tuple[str, str]], knowledge, complete: Callable[..., dict | None]) -> dict[int, str]:
    """Какие заметки (о чём, что) противоречат данным игры: {номер с 0: почему}. Данные — поиск по базе знаний
    (люди, места, общеизвестное; companion/knowledge.py). Ни для одной заметки данных нет — модель не спрашиваем."""
    blocks, any_evidence = [], False
    for i, (subject, text) in enumerate(facts, 1):
        # Сначала — о ком/о чём заметка, потом — по всему тексту: иначе на «Лукан работает в «Товарах Белетора»» данные
        # были про лавку и Вайтран, а запись о самом Лукане до сверки не доходила.
        found = knowledge.search(subject, VERIFY_EVIDENCE) + knowledge.search(f"{subject} {text}", VERIFY_EVIDENCE)
        evidence = list(dict.fromkeys(t for kind, t in found if not kind.startswith("book")))[:VERIFY_EVIDENCE + 2]
        any_evidence |= bool(evidence)
        blocks.append(f"{i}. {subject}: {text}\nДанные игры:\n"
                      + ("\n".join(f"- {t[:VERIFY_CHARS]}" for t in evidence) or "- нет"))
    if not any_evidence:
        return {}
    result = complete(VERIFY_SYSTEM, "\n\n".join(blocks), title=f"Сверка фактов: {len(facts)}")
    if not isinstance(result, dict):
        return {}
    why = {str(k): str(v) for k, v in (result.get("why") or {}).items()}
    return {int(n) - 1: why.get(str(n), "") for n in result.get("wrong") or []
            if str(n).isdigit() and 1 <= int(n) <= len(facts)}


class Diarist:
    def __init__(self, store: MemoryStore, complete: Callable[..., dict | None], cfg: DiaristConfig | None = None,
                 player_name: Callable[[], str] = lambda: "Владос", knowledge=None):
        self.cfg = cfg or DiaristConfig()
        self.store = store
        self.complete = complete
        self.player_name = player_name
        self.knowledge = knowledge  # companion.knowledge.Knowledge — новые факты сверяем с данными игры
        self._wake = threading.Event()
        self._place_changed = False
        self._pending_since: float | None = None  # когда заметили первую неописанную запись
        self._busy = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="diarist").start()
        log.info("Летописец: дневник по главам (уход из места, %d реплик, %d мин)",
                 self.cfg.big_dialogue, self.cfg.chapter_seconds // 60)

    def place_changed(self) -> None:
        """Ушли из места — глава кончилась, если в ней что-то было."""
        self._place_changed = True
        self._wake.set()

    def _run(self) -> None:
        while True:
            self._wake.wait(self.cfg.check_every)
            self._wake.clear()
            try:
                self.check()
            except Exception:
                log.exception("Летописец: ошибка")

    def check(self, force: bool = False) -> bool:
        """Записать главу, если пора. True — записали."""
        if not self.store.is_open or not self._busy.acquire(blocking=False):
            return False
        try:
            place_changed, self._place_changed = self._place_changed, False
            covered, _ = self.store.diary_last_covered()
            rows = self.store.journal_since(covered)
            if not rows:
                self._pending_since = None
                return False
            self._pending_since = self._pending_since or time.monotonic()
            dialogue = sum(1 for r in rows if r[2] in ("player", "gosling"))
            c = self.cfg
            due = (force or len(rows) >= c.big_total or dialogue >= c.big_dialogue
                   or (len(rows) >= c.min_entries
                       and (place_changed or time.monotonic() - self._pending_since >= c.chapter_seconds)))
            if not due:
                return False
            return self._write(rows)
        finally:
            self._busy.release()

    def _write(self, rows: list[tuple]) -> bool:
        name = self.player_name() or "Владос"
        heard = [r for r in rows if r[2] == "heard"][-self.cfg.max_heard:]
        lines = [self._line(r, name) for r in rows if r[2] != "heard" or r in heard][-self.cfg.max_lines:]
        facts = [f"- {subject}: {text}" + (f" ({source})" if source else "")
                 for subject, text, source in self.store.facts()[-self.cfg.known_facts:]]
        goals = self.store.active_goals()
        user = (f"Записи с прошлого раза:\n" + "\n".join(lines)
                + "\n\nЧто ты уже знаешь (факты):\n" + ("\n".join(facts) or "пока ничего")
                + "\n\nАктивные цели, договорённости и что знаешь о Владосе (id: вид — текст):\n"
                + ("\n".join(f"{gid}: {kind} — {text}" for gid, kind, text in goals) or "нет"))
        t = time.perf_counter()
        result = self.complete(SYSTEM, user, title=f"Летописец: глава из {len(rows)} записей")
        if not isinstance(result, dict) or not str(result.get("diary", "")).strip():
            log.warning("Летописец: модель не вернула запись — попробуем позже")
            return False
        last = rows[-1]
        # Где прошла глава — по последней реплике, а не событию: последним часто идёт «новая локация: …».
        spoken = next((r for r in reversed(rows) if r[2] != "event" and r[4]), last)
        where = Where(spoken[4] or "", last[1])
        self.store.add_diary(str(result["diary"]).strip(), where, journal_to=last[0])
        new_facts = [f for f in result.get("facts") or [] if isinstance(f, dict)]
        if self.knowledge and new_facts:
            # Летописец закреплял выдумки из разговора («Лукан в лавке Белетора») — сверяем с данными игры.
            wrong = verify_facts([(str(f.get("subject", "")), str(f.get("text", ""))) for f in new_facts],
                                 self.knowledge, self.complete)
            for i, why in wrong.items():
                log.info("Летописец: факт не сходится с данными игры — не записываю: %s: %s (%s)",
                         new_facts[i].get("subject"), new_facts[i].get("text"), why)
            new_facts = [f for i, f in enumerate(new_facts) if i not in wrong]
        for f in new_facts:
            self.store.add_fact(str(f.get("subject", "")), str(f.get("text", "")), str(f.get("source", "")), where)
        new_goals = [g for g in result.get("goals_new") or [] if isinstance(g, dict) and g.get("kind") in GOAL_KINDS]
        for g in new_goals:
            self.store.add_goal(g["kind"], str(g.get("text", "")), where)
        known = {gid for gid, _, _ in goals}
        for status, key in (("done", "goals_done"), ("dropped", "goals_dropped")):
            for gid in result.get(key) or []:
                if isinstance(gid, int) and gid in known:
                    self.store.close_goal(gid, status)
        log.info("Летописец %.1f с: глава из %d записей -> дневник, фактов %d, целей %d | %s",
                 time.perf_counter() - t, len(rows), len(new_facts), len(new_goals), result["diary"])
        self._pending_since = None
        return True

    @staticmethod
    def _line(row: tuple, name: str) -> str:
        _id, days, kind, text, place = row
        who = {"player": f"{name}:", "gosling": "Ты (Гослинг):", "event": "Событие:", "heard": "Слышал рядом —"}
        return f"[{game_time(days)}, {place or '?'}] {who.get(kind, kind)} {text}"
