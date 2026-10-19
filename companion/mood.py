"""Настроение Гослинга: быстрое состояние на минуты — из событий игры, без модели.

Эмоция реплики выбиралась заново каждый раз: погиб Владос — через минуту Гослинг снова беззаботен. Теперь события
сдвигают четыре составляющие настроения (напряжение, радость, подавленность, раздражение), и они сами затухают —
каждая со своей скоростью. Таблица — по виду события (Change.kind), общая, без частных случаев. Загрузка сейва —
настроение с нуля (после гибели Владоса — с подавленности).

Разговор тоже двигает настроение: смысл фразы коду не понять («ну ты и тормоз» — обида или подколка), поэтому модель в
ответе Владосу отмечает, задела ли его реплика и как (поле felt: радость, обида, злость, страх, утешение; сила 1..3), а
сдвиг — по таблице FELT_MOOD, не больше ±0,3 за реплику.

Таблица одна на всех, а у Гослинга есть душа (companion/memory/soul.py): что его радует, пугает, раздражает,
печалит — с поводами, словами из событий игры («паук», «топор»). Событие с таким словом сдвигает настроение сильнее
(PERSONAL): боится пауков — бой с пауком пугает больше; любит топоры — подаренный топор радует больше.
Забрали вещь (taken) — не радует; забрали то, что радует по душе, — лёгкая грусть (LOSS).
Повтор того же события (Change.subject: тот же враг вдали, тот же бой), пока его вклад не улёгся, не прибавляет
заново, а освежает вклад до прежнего (Mood._from).

Модели — строка «Твоё настроение: …» в обстановке (с причиной: что и когда было; причина — суть события,
Change.gist, без «Перед этим» и подсказок модели, иначе они висят в каждом запросе, пока настроение не уляжется). Голосу — эмоция по умолчанию: модель
ответила нейтрально, а настроение сильное — звучит эмоция настроения (теги ElevenLabs). Кроме радости: в восторге
от нового места Гослинг и деловой совет («до башни двести метров») говорил радостно — модель настроение видит, и
если выбрала ровный тон, так и звучит. Настройки — секция mood.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from companion.game.items import words
from companion.game.state import Change

LOADED = {"loaded", "died"}  # загрузка сейва: что было до неё — другое прошлое


@dataclass(frozen=True)
class Component:
    half_life: float                 # секунд — за столько ослабевает вдвое
    levels: tuple[str, str, str]     # слегка / заметно / сильно
    emotion: str | None              # эмоция голоса, когда это главное в настроении; None — голосу не навязываем


COMPONENTS = {
    "tension": Component(180, ("немного насторожен", "на взводе", "весь на нервах"), "fear"),
    "joy": Component(600, ("в неплохом настроении", "в приподнятом настроении", "в восторге"), None),
    "gloom": Component(900, ("немного не в духе", "подавлен", "совсем пал духом"), "sad"),
    "irritation": Component(600, ("слегка раздражён", "раздражён", "злишься"), "anger"),
}

# Как события сдвигают настроение: вид события -> {составляющая: на сколько} (шкала 0..1).
EVENT_MOOD: dict[str, dict[str, float]] = {
    "combat_started": {"tension": 0.5},
    "threat": {"tension": 0.3},
    # Беда гасит радость: иначе «пал духом» соседствует с «в приподнятом настроении — подземелье зачищено».
    "low_health": {"tension": 0.6, "joy": -0.2},     # Владос тяжело ранен
    "hurt_self": {"tension": 0.6, "gloom": 0.2, "joy": -0.2},  # сам тяжело ранен / упал без сил
    "person": {"tension": 0.3, "gloom": 0.3, "joy": -0.2},     # знакомый напал или убит
    "novel": {"tension": 0.2, "joy": 0.2},           # впервые видит такое существо
    "player_died": {"gloom": 0.8, "tension": 0.3, "joy": -1.0},  # Владос погиб
    "died": {"gloom": 0.6},                          # загрузились после гибели Владоса
    "combat_ended": {"tension": -0.4, "joy": 0.2},   # отбились
    "cleared": {"joy": 0.5},
    "discovered": {"joy": 0.4},
    "power": {"joy": 0.4},
    "gift": {"joy": 0.5},
    "quest": {"joy": 0.2},
    "profile": {"joy": 0.3},
    "looted": {"joy": 0.1},
    "crime": {"irritation": 0.3},                    # Владос ворует или нарушает закон
}

# Повод из души: чувство -> составляющая, на сколько сильнее (сверх таблицы).
FEELINGS = {"радость": "joy", "страх": "tension", "неприязнь": "irritation", "грусть": "gloom"}
PERSONAL = 0.35
# Не с ним самим: что говорят NPC, что Владос подобрал или нашёл в сундуке (железный щит в его сумке — не подарок).
NOT_PERSONAL = {"overheard", "item", "looted"}
# Потеря (taken — Владос забрал вещь): своё в таблице не радует, а забрали то, что радует по душе, — лёгкая грусть
# («немного не в духе»), не больше; страх и неприязнь к забранному не задевают.
LOSS = {"taken"}
LOSS_FEELINGS = {"радость": "gloom"}
LOSS_PERSONAL = 0.3

# Разговор: как реплика Владоса задела (чувство из ответа модели, сила 1..3) -> сдвиг на единицу силы. Не больше
# ±0,3 за реплику: одна фраза не переворачивает настроение. Утешение снимает подавленность и напряжение.
# Чувство -> (что растёт, что сначала гасится). Сила реплики (0,15 на единицу, не больше FELT_CAP) сперва тратится на
# противоположное, и только остаток — в своё: «дурак», потом «друг» — снова ровное, а не «не в духе» <-> «в неплохом»
# туда-обратно. Словами за раз снимается не больше половины чувства (FELT_CUT): подавленность после гибели Владоса
# уходит постепенно, а не за две фразы. И словами не выше FELT_CEILING — сильнее задевают только события игры.
# (Шаг 0,15: при 0,1 обида силой 1–2 не дотягивала до порога показа 0,25.)
FELT_MOOD: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "радость": ("joy", ("gloom", "irritation")),
    "обида": ("gloom", ("joy",)),
    "злость": ("irritation", ("joy",)),
    "страх": ("tension", ()),
    "утешение": (None, ("gloom", "tension")),
}
FELT_STEP = 0.15
FELT_CAP = 0.3       # за одну реплику
FELT_CUT = 0.5       # снимается за раз не больше этой доли чувства
FELT_CEILING = 0.5   # выше словами не поднять: «подавлен», «в приподнятом», но не «пал духом» и не «в восторге»

SHOWN = 0.25      # слабее — не показываем
STRONG = 0.5      # главное в настроении — его эмоция идёт в голос
LEVELS = (0.4, 0.7)  # границы «слегка / заметно / сильно»


def ago(seconds: float) -> str:
    if seconds < 60:
        return "только что"
    minutes = round(seconds / 60)
    return f"{minutes} мин назад"


class Mood:
    def __init__(self, clock=time.time, soul: Callable[[], list[tuple[str, str, dict | None]]] | None = None):
        self.clock = clock
        self.soul = soul or (lambda: [])  # душа с поводами: [(ключ, текст, {чувство: [слова]} | None)]
        self._lock = threading.Lock()
        self._value: dict[str, float] = {}
        self._at: dict[str, float] = {}               # когда значение записано
        self._cause: dict[str, tuple[str, float, str]] = {}  # последняя причина: текст события, когда, его subject
        # Сколько дало событие (по subject) составляющей и когда: повтор того же события, пока его вклад не улёгся,
        # не прибавляет заново, а освежает вклад до прежнего — злокрыс вдали, объявленный раз в минуту, не копит
        # «весь на нервах».
        self._from: dict[tuple[str, str], tuple[float, float]] = {}

    def _now(self, name: str, now: float) -> float:
        value, at = self._value.get(name, 0.0), self._at.get(name, now)
        return value * 0.5 ** ((now - at) / COMPONENTS[name].half_life)

    def _personal(self, change: Change, soul: list[tuple[str, str, dict | None]]) -> list[tuple[str, float, str]]:
        """Что в событии задело Гослинга лично: [(составляющая, на сколько, причина)] — по поводам из души."""
        if change.kind in NOT_PERSONAL:
            return []
        loss = change.kind in LOSS
        feelings, delta = (LOSS_FEELINGS, LOSS_PERSONAL) if loss else (FEELINGS, PERSONAL)
        have = words(change.gist)
        out = []
        for key, _text, feel in soul:
            for feeling, triggers in (feel or {}).items():  # None — поводов ещё не спрашивали (старая запись)
                name = feelings.get(feeling)
                hit = next((t for t in triggers if (want := words(t))
                            and all(any(w.startswith(m) for w in have) for m in want)), None)
                if name and hit:
                    out.append((name, delta, f"{change.gist} — {'жаль' if loss else 'задело лично'} ({key})"))
        return out

    def notify(self, changes: list[Change]) -> None:
        now = self.clock()
        try:
            soul = self.soul()
        except Exception:  # noqa: BLE001 — без души настроение всё равно считается по таблице
            soul = []
        with self._lock:
            if any(c.kind in LOADED for c in changes):
                self._value, self._at, self._cause, self._from = {}, {}, {}, {}
            for change in changes:
                key = change.subject or change.gist
                # Сила события — по таблице, а тревожное — с поправкой на опасность врага (Change.weight); повод из
                # души — сверх того, в тот же вклад события (и причина — его, она точнее).
                total: dict[str, list] = {name: [delta * change.weight if delta > 0 else delta, change.gist]
                                          for name, delta in EVENT_MOOD.get(change.kind, {}).items()}
                for name, delta, cause in self._personal(change, soul):
                    total.setdefault(name, [0.0, cause])[0] += delta
                    total[name][1] = cause
                for name, (delta, cause) in total.items():
                    if delta <= 0:
                        self._value[name] = max(0.0, self._now(name, now) + delta)
                        self._at[name] = now
                        continue
                    add = max(0.0, delta - self._left(key, name, now))
                    self._value[name] = min(1.0, self._now(name, now) + add)
                    self._at[name] = now
                    self._from[(key, name)] = (delta, now)
                    # Причина — что больше всего держит настроение: новое событие её перехватывает, только если
                    # прибавило не меньше, чем ещё держится от прежней (злокрыс рядом с великанами — не причина).
                    was = self._cause.get(name)
                    if not was or was[2] == key or add >= self._left(was[2], name, now):
                        self._cause[name] = (cause, now, key)

    def _left(self, key: str, name: str, now: float) -> float:
        """Сколько от вклада события key в составляющую name ещё держится."""
        was = self._from.get((key, name))
        return was[0] * 0.5 ** ((now - was[1]) / COMPONENTS[name].half_life) if was else 0.0

    def felt(self, feeling: str, strength: int, said: str) -> None:
        """Реплика Владоса задела (модель отметила в ответе): сдвиг настроения, причина — его слова."""
        now = self.clock()
        cause = f"Владос сказал: «{said[:80]}{'…' if len(said) > 80 else ''}»"
        if feeling not in FELT_MOOD:
            return
        grow, against = FELT_MOOD[feeling]
        budget = min(FELT_CAP, FELT_STEP * strength)
        with self._lock:
            for name in against:  # сперва гасим противоположное — не больше половины за раз
                current = self._now(name, now)
                cut = min(budget, current * FELT_CUT)
                self._value[name], self._at[name] = current - cut, now
                budget -= cut
            if grow and budget > 0.01:  # остаток — в своё, но словами не выше потолка (и не срезая события)
                current = self._now(grow, now)
                self._value[grow] = min(max(current, FELT_CEILING), current + budget)
                self._at[grow] = now
                self._cause[grow] = (cause, now, "")

    def state(self) -> dict[str, float]:
        now = self.clock()
        with self._lock:
            return {name: round(self._now(name, now), 2) for name in COMPONENTS}

    def describe(self) -> str:
        """Строка в обстановку: «Твоё настроение: на взводе — начался бой: Волк (только что)»."""
        now = self.clock()
        with self._lock:
            parts, told = [], set()
            for name, value in sorted(((n, self._now(n, now)) for n in COMPONENTS), key=lambda x: -x[1]):
                if value < SHOWN:
                    continue
                level = COMPONENTS[name].levels[sum(value >= edge for edge in LEVELS)]
                cause = self._cause.get(name)
                if cause and cause[0] not in told:  # одна причина — один раз («погиб» и на нервах, и пал духом)
                    told.add(cause[0])
                    level += f" — {cause[0]} ({ago(now - cause[1])})"
                parts.append(level)
        # «Не объявляй без повода» модель понимала как «спрячь»: после гибели Владоса на «как ты?» — «да нормально».
        return ("Твоё настроение сейчас (из того, что было): " + ("; ".join(parts) if parts else "ровное")
                + ". Пусть оно слышно в том, как ты говоришь; спросят, как ты, — отвечай по нему.")

    def emotion(self) -> str | None:
        """Эмоция голоса по умолчанию: главное в настроении, если оно сильное и голосу его навязываем; иначе
        None."""
        state = self.state()
        name, value = max(state.items(), key=lambda x: x[1])
        return COMPONENTS[name].emotion if value >= STRONG else None
