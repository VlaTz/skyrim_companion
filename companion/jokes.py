"""Шутки Гослинга: база заготовок, подбор по обстановке, пополнение в фоне и шутки из общей истории.

База — characters/gosling/jokes.yaml (просмотрена игроком) и jokes_auto.yaml (Гослинг дописал сам в игре,
new: true — ждёт просмотра). Оба файла перечитываются, когда меняются, — перезапуск сервиса не нужен.

Метки обстановки — из того, что сервис и так знает (Jokes.scene_tags): город и тип места (справка о местах), дикие
земли, ночь и погода, кто рядом (стража, драугры, Назим), бой и ранение, сумка (перегруз), гильдии и привычки
Владоса, и само событие-повод (гибель, кража, Крик). Подбор (JokeBook.pick): у каждой метки вес — чем реже она
в базе, тем больше (Рифтен важнее «бой»); метки-условия (город, погода, кто такой Владос…) должны совпасть;
несказанные раньше — выше; недавно сказанные (COOLDOWN_DAYS игровых дней или COOLDOWN_SECONDS) — не предлагаем;
немного случайности, чтобы при одной обстановке не выходили одни и те же. Что сказано — память персонажа
(encounters, категория «шутка», по сейву, как всё).

Пополнение (JokeWriter): несказанных заготовок к обстановке осталось меньше jokes.refill_below — сильная модель в
фоне дописывает новые под самые редкие метки обстановки (с фактами лора и карточкой места) в jokes_auto.yaml. Не
чаще jokes.refill_gap. Тот же JokeWriter — у tools/generate_jokes.py.

Общая история (Jokes.history): не из базы, а из памяти — сколько раз и от кого Владос погибал в этой ветке
сейва, привычки по статистике игры (украдено, взломано, поглощено душ драконов…). Сами не кончаются.

Модель получает 3–4 заготовки и 0–2 строки истории и сама решает, обыграть ли одну своими словами (поле joke в
ответе — id заготовки или строки истории) или нет.
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from companion.config import JokesConfig
from companion.game.places import PlaceBook
from companion.game.profile import HABITS
from companion.game.state import Change, GameState, race_name
from companion.memory.keeper import MemoryKeeper

log = logging.getLogger(__name__)

PICK = 4                  # заготовок в запрос
HISTORY_PICK = 2          # строк общей истории в запрос
COOLDOWN_DAYS = 3         # игровых дней — сказанную не предлагаем
COOLDOWN_SECONDS = 7200   # и не раньше чем через 2 часа игры (игровые дни при быстром перемещении летят)
NEVER_SAID_BONUS = 0.5    # несказанная ни разу — выше
JITTER = 0.4              # случайная добавка к весу — разнообразие при одной обстановке
SAID = "шутка"            # категория в памяти встреч
REFILL_KEY_TAGS = 2       # под сколько самых редких меток обстановки дописывать новые
WRITER_MODEL = "anthropic/claude-sonnet-5.5"  # почти как Opus, вдвое дешевле (2026-10-15_шутки.md)

# Повод (Change.kind) -> метки. Место, погода, кто рядом — из обстановки (scene_tags), тут — только само событие.
EVENT_TAGS: dict[str, set[str]] = {
    "combat_started": {"бой"}, "combat_ended": {"бой"}, "low_health": {"ранение"}, "died": {"гибель", "загрузка"},
    "looted": {"добыча", "сундук"}, "item": {"добыча"}, "cleared": {"подземелье", "добыча"}, "crime": {"кража"},
    "power": {"крик"}, "quest": {"задание"}, "quest_misc": {"задание"}, "wilderness": {"дикие земли", "дорога"},
    "person": {"бой"}, "threat": {"бой"},
}
TEXT_TAGS = [(re.compile(p, re.I), tag) for p, tag in [
    (r"Крик|слово силы|Ту'ум", "крик"), (r"дракон|dragon", "дракон"), (r"взломал", "взлом"),
    (r"украл|обчистил|угнал", "кража"), (r"заклинани", "магия"), (r"тюрьм", "тюрьма"),
]]
# Тип места из справки -> метка базы
KIND_TAGS = {"город": "город", "городок": "город", "поселение": "деревня", "двемерские механизмы": "двемерские руины",
             "логово зверей": "пещера"}
# Кто рядом: часть имени или расы -> метка
NEARBY_TAGS = [("стражник", "стража"), ("драугр", "драугры"), ("дракон", "дракон"), ("dragon", "дракон"),
               ("великан", "великан"), ("giant", "великан"), ("фалмер", "фалмеры"), ("паук", "пауки"),
               ("грязевой краб", "грязевой краб"), ("каджит", "каджиты"), ("нищ", "нищий"), ("хускарл", "хускарл")]
# Гильдии профиля -> метка
GUILD_TAGS = {"Гильдия воров": "вор", "Соратники": "Соратник", "Круг Соратников": "оборотень",
              "Коллегия Винтерхолда": "маг", "Тёмное Братство": "Тёмное Братство", "Братья Бури": "Братья Бури",
              "Имперский легион": "Легион", "вампиры Волкихара": "вампир"}
# Статистика -> метка: (название, с какого значения)
STAT_TAGS = [("Werewolf Transformations", 1, "оборотень"), ("Dragon Souls Collected", 1, "Довакин"),
             ("Items Stolen", 10, "вор"), ("Pockets Picked", 5, "вор")]
OVERLOAD = 12             # оружия и брони в сумке — «тащит весь хлам»
# Метки-условия: есть у шутки метка этого рода — хоть одна такая должна быть в обстановке. Иначе в таверне Рифтена
# выходила маркартская «кровати из камня» (совпали «таверна» и «ночь»), а «ты в тенях сидишь» — не вору.
CONDITIONS = [
    {"Вайтран", "Рифтен", "Солитьюд", "Виндхельм", "Маркарт", "Винтерхолд", "Данстар", "Фолкрит", "Морфал",
     "Ривервуд", "Хелген", "Высокий Хротгар"},
    {"вор", "Соратник", "маг", "оборотень", "вампир", "Довакин", "Тёмное Братство", "Братья Бури", "Легион"},
    {"гибель", "загрузка", "перегруз", "кража", "взлом", "тюрьма"},
    {"снег", "дождь"},
    {"Назим", "Хеймскр", "Белетор", "Алик'р"},
]

# Общая история: привычка по статистике игры -> (с какого значения, к каким меткам обстановки). Подпись — HABITS.
HISTORY_STATS = [
    ("Locks Picked", 5, {"взлом", "сундук", "кража", "тишина"}),
    ("Items Stolen", 5, {"кража", "лавка", "торговля", "стража", "вор", "тишина"}),
    ("Pockets Picked", 3, {"кража", "стража", "город"}),
    ("Times Jailed", 1, {"тюрьма", "стража"}),
    ("Murders", 1, {"стража", "город"}),
    ("Bribes", 2, {"стража"}),
    ("Dragon Souls Collected", 1, {"дракон", "крик"}),
    ("Werewolf Transformations", 1, {"оборотень", "ночь"}),
    ("Potions Mixed", 10, {"зелья", "магия"}),
    ("Books Read", 10, {"тишина", "храм"}),
    ("Locations Discovered", 10, {"дорога", "тишина", "дикие земли"}),
]
DEATH_TAGS = {"гибель", "загрузка", "ранение", "бой", "дракон", "тишина"}
KILLER = re.compile(r"его убил (.+?)(?:, |; |\)\.|\)$|$)")

# Темы для генерации: (название, метки, опорные факты — только общеизвестное в Скайриме)
TOPICS: list[tuple[str, list[str], str]] = [
    ("Вайтран", ["Вайтран", "город"], "Облачный район и Назим; Соратники в Йоррваскре; Златолист; Хеймскр проповедует "
     "Талоса; Драконий Предел, ярл Балгруф; Гарцующая кобыла; Белетор продаёт всё, что угодно"),
    ("Рифтен", ["Рифтен", "город"], "Гильдия воров и Крысиная нора; Мавен Чёрный Вереск и медовуха «Чёрный вереск»; "
     "каналы и запах; таверна «Пчела и жало»; стража берёт взятки; приют Грелод Добросердечной"),
    ("Солитьюд", ["Солитьюд", "город"], "Синий дворец; казнь Роггвира у ворот; Коллегия бардов; посольство Талмора; "
     "город на арке над морем; столица имперцев, генерал Туллий"),
    ("Виндхельм", ["Виндхельм", "город", "холод", "снег"], "Ульфрик Буревестник и Братья Бури; Серый квартал, данмеры "
     "и аргониане в порту; лютый холод; таверна «Очаг и свеча»; старейший город Скайрима"),
    ("Маркарт", ["Маркарт", "город"], "всё из камня, город построен в двемерских руинах; Изгои; шахта Сидна — "
     "тюрьма; клан Серебряная Кровь; «здесь все что-то скрывают»"),
    ("Винтерхолд", ["Винтерхолд", "город", "снег"], "половина города обрушилась в море в Великий обвал; Коллегия "
     "магов на скале; местные не любят магов; таверна «Замёрзший очаг»"),
    ("Данстар", ["Данстар", "город", "холод"], "жителей мучают кошмары; шахты; холодное море; Тёмное Братство где-то "
     "неподалёку"),
    ("Фолкрит", ["Фолкрит", "город"], "огромное кладбище — мёртвых больше, чем живых; хвойный лес; тихий городок"),
    ("Морфал", ["Морфал", "город", "дождь"], "болота и туман; ярл Идгрод видит видения; вампиры; мрачные жители"),
    ("Ривервуд", ["Ривервуд", "деревня"], "деревня у реки, лесопилка; Свен и Фендал соперничают за Камиллу; лавка "
     "Лукана, у которого украли золотой коготь; над деревней Ветреный пик"),
    ("склеп драугров", ["склеп драугров", "драугры", "подземелье"], "драугры встают из саркофагов; ловушки — "
     "маятники, плиты, дротики; загадки со столбами и когтями; урны с костной мукой; паутина"),
    ("двемерские руины", ["двемерские руины", "подземелье", "фалмеры"], "паровые центурионы и пауки-механизмы; "
     "двемеры исчезли неизвестно куда; трубы, пар, шестерни; фалмеры и хоркеры"),
    ("пещера", ["пещера", "подземелье"], "злокрысы, пауки, бандиты; сырость и капает вода; темно"),
    ("шахта", ["шахта"], "руда, кирка, рудокопы; темно и тесно"),
    ("великаны", ["великан", "лагерь великанов"], "мамонты и пастухи-великаны; удар великана отправляет в небо; "
     "сыр из мамонтова молока"),
    ("драконы", ["дракон", "логово дракона", "крик"], "драконы вернулись; стена слов; Довакин поглощает души; "
     "драконьи кости и чешуя"),
    ("бандиты", ["дикие земли", "бой", "дорога"], "бандиты грабят прохожих и сидят в старых фортах; «кошелёк или "
     "жизнь»"),
    ("таверна", ["таверна", "выпивка", "еда"], "медовуха, барды, сладкий рулет, похлёбка; спать за 10 монет"),
    ("лавка", ["лавка", "торговля"], "торговцы скупают всё, но дёшево; у торговца вечно мало золота"),
    ("храм", ["храм"], "Девять божеств, жрецы, благословение у алтаря; Талос под запретом Талмора"),
    ("тюрьма", ["тюрьма", "стража"], "заплатить штраф или сидеть; отмычка в тюфяке; конфискованные вещи"),
    ("дорога", ["дорога", "дикие земли", "тишина", "горы"], "долгие переходы, повозки, волки и медведи у дороги, "
     "горы и перевалы"),
    ("гибель", ["гибель", "загрузка", "ранение"], "Владос погиб и вернулся к сохранению; хорошо бы в этот раз не "
     "так; Совнгард подождёт"),
    ("перегруз", ["перегруз", "добыча", "торговля"], "Владос тащит весь хлам, железные шлемы, двемерские тарелки и "
     "вилки; ползёт как улитка"),
    ("кража", ["кража", "вор", "взлом"], "Владос ворует, вскрывает замки, ломает отмычки; стража видит всё"),
    ("сундук", ["сундук", "добыча"], "в сундуке опять три септима и кусок сыра; урны и мешки"),
    ("бой", ["бой", "ранение"], "Владос лезет в драку, пьёт зелья посреди боя, враги бьют в спину"),
    ("зелья и магия", ["зелья", "магия", "маг"], "алхимия из всего подряд, зелья с странным вкусом, заклинания, "
     "магия огня в помещении"),
    ("погода и ночь", ["снег", "холод", "ночь", "дождь"], "метель, мороз, ночь, хочется к очагу"),
    ("Владос-вор", ["вор", "кража", "Рифтен"], "Владос в Гильдии воров, Рифтен, тени, «ничего не трогай»"),
    ("Владос-Соратник", ["Соратник", "оборотень"], "Соратники, Йоррваскр, Круг, тайна оборотней"),
    ("Владос-маг", ["маг", "магия", "Винтерхолд"], "Коллегия магов, Архимаг, заклинания и книги"),
    ("Владос-Довакин", ["Довакин", "крик", "дракон"], "Седобородые, Высокий Хротгар, 7000 ступеней, Ту'ум"),
]

WRITER_SYSTEM = """Ты пишешь заготовки шуток для Гослинга — спутника игрока в Скайриме. Гослинг: весёлый молодой
норд, бывший картограф из Солитьюда, любопытный, немного самоуверенный, по-доброму подкалывает Владоса (игрока).
Заготовка — одна короткая фраза (до 15 слов), которую Гослинг обыграет вслух к месту. Разнообразь форму:
подколка Владосу, наблюдение, байка, местная поговорка, вопрос; не начинай всё с «говорят». Смешное — в
неожиданном повороте в конце фразы; лучше меньше, но смешно. Шути о настоящем Скайриме: опирайся на данные
факты и общеизвестное в игре, не выдумывай имён, мест и событий. Не выдумывай и общего прошлого с Владосом
(«в прошлый раз ты тут…») — его может не быть. Гослинг живёт в этом мире — никаких компьютеров, игр, сохранений
как технической вещи (про гибель Владоса и возврат к сохранению Гослинг знает и говорит об этом прямо, как о
странности мира). Без пошлости и без злобы к народам. Не повторяй уже имеющиеся шутки и не пересказывай их.
Отвечай строго одним JSON-объектом без пояснений и markdown:
{"groups": [{"group": "<название группы>", "jokes": [{"id": "<латиницей-через-дефис>", "text": "...",
"tags": ["...", ...]}, ...]}, ...]}
tags — только из словаря меток ниже; обязательно метки группы и ещё подходящие (бой, ночь, таверна...). Метки
«гибель», «загрузка», «кража», «взлом», «перегруз», «тюрьма», погоду (снег, дождь) и кто такой Владос (вор, маг,
Соратник, Тёмное Братство…) ставь, только если шутка именно об этом: по ним её предложат лишь в таких случаях."""

AUTO_HEADER = """# Шутки, которые Гослинг дописал сам в игре (companion/jokes.py, JokeWriter): несказанных заготовок к
# обстановке осталось мало — сильная модель написала новые. Формат и словарь меток — как в jokes.yaml.
# Не смешно или не к месту — удали строку; удачные можно перенести в jokes.yaml (или оставить тут).
jokes:
"""


def vocabulary(header: str) -> set[str]:
    """Словарь меток из шапки jokes.yaml («#     место:   город, деревня, ...» и продолжения строк)."""
    words, inside = set(), False
    for line in header.splitlines():
        if m := re.match(r"#\s+(место|город|рядом|событие|время|Владос):\s+(.*)", line):
            inside = True
            words.update(w.strip() for w in m[2].split(","))
        elif inside and (m := re.match(r"#\s{10,}(\S.*)", line)):
            words.update(w.strip() for w in m[1].split(","))
        else:
            inside = False
    return {w for w in words if w}


@dataclass(frozen=True)
class Joke:
    id: str
    text: str
    tags: frozenset[str]


class JokeBook:
    """База заготовок из файлов (главный — jokes.yaml, словарь меток в его шапке; auto — дописанное в игре);
    перечитывается при изменении файлов. Потокобезопасно."""

    def __init__(self, path: Path, auto_path: Path | None = None):
        self.path = path
        self.auto_path = auto_path
        self._lock = threading.Lock()
        self._mtimes: tuple = ()
        self._jokes: list[Joke] = []
        self._weight: dict[str, float] = {}
        self.vocab: set[str] = set()

    @property
    def tags(self) -> set[str]:
        return set(self._load())

    @property
    def jokes(self) -> list[Joke]:
        self._load()
        return list(self._jokes)

    def has(self, joke_id: str) -> bool:
        self._load()
        return any(j.id == joke_id for j in self._jokes)

    def _load(self) -> dict[str, float]:
        with self._lock:
            paths = [p for p in (self.path, self.auto_path) if p]
            mtimes = tuple(p.stat().st_mtime if p.is_file() else 0.0 for p in paths)
            if mtimes == self._mtimes:
                return self._weight
            jokes: list[Joke] = []
            for p in paths:
                if not p.is_file():
                    continue
                try:
                    text = p.read_text(encoding="utf-8")
                    data = yaml.safe_load(text) or {}
                    jokes += [Joke(str(j["id"]), str(j["text"]), frozenset(map(str, j.get("tags") or [])))
                              for j in data.get("jokes") or [] if j.get("id") and j.get("text")]
                    if p == self.path:
                        self.vocab = vocabulary(text)
                except (OSError, yaml.YAMLError, KeyError, TypeError, AttributeError) as e:
                    log.warning("Шутки: не прочитать %s (%s) — остаются прежние", p.name, e)
                    self._mtimes = mtimes
                    return self._weight
            # Вес метки — по редкости (idf): «Рифтен» у 17 шуток из 250 весит больше, чем «бой» у 37.
            df = Counter(t for j in jokes for t in j.tags)
            self._weight = {t: math.log((len(jokes) + 1) / (c + 1)) + 0.1 for t, c in df.items()}
            self._jokes, self._mtimes = jokes, mtimes
            log.info("Шутки: %d заготовок, %d меток", len(jokes), len(df))
            return self._weight

    def weight(self, tag: str) -> float:
        return self._load().get(tag, 0.0)

    def eligible(self, tags: set[str], said: dict[str, tuple[float, float | None]], days: float | None) -> list[Joke]:
        """Заготовки к обстановке tags, кроме недавно сказанных."""
        self._load()
        now, out = time.time(), []
        for j in self._jokes:
            if not j.tags & tags or any(j.tags & group and not j.tags & group & tags for group in CONDITIONS):
                continue
            if j.id in said and recently(said[j.id], now, days):
                continue
            out.append(j)
        return out

    def pick(self, tags: set[str], said: dict[str, tuple[float, float | None]], days: float | None,
             n: int = PICK) -> list[Joke]:
        """Заготовки к обстановке tags, лучшие первыми. said — id -> (когда сказана, игровая дата)."""
        weight = self._load()
        scored = [(sum(weight.get(t, 0) for t in j.tags & tags) + (0 if j.id in said else NEVER_SAID_BONUS)
                   + random.random() * JITTER, j) for j in self.eligible(tags, said, days)]
        scored.sort(key=lambda s: -s[0])
        return [j for _s, j in scored[:n]]


def times(n: int) -> str:
    """«2 раза», «5 раз», «21 раз»."""
    word = "раза" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "раз"
    return f"{n} {word}"


def recently(said: tuple[float, float | None], now: float, days: float | None) -> bool:
    real, game_days = said
    return now - real < COOLDOWN_SECONDS or (days is not None and game_days is not None
                                             and days - game_days < COOLDOWN_DAYS)


class JokeWriter:
    """Пишет новые заготовки сильной моделью и дописывает их в файл (new: true). Для tools/generate_jokes.py и
    пополнения в фоне."""

    def __init__(self, book: JokeBook, complete: Callable[..., dict | None], model: str = WRITER_MODEL):
        self.book = book
        self.complete = complete  # CompanionLLM.complete_json
        self.model = model

    def write(self, groups: list[tuple[str, list[str], str]], out: Path, per_group: int = 6, batch: int = 4,
              report: Callable[[str], None] = log.info) -> int:
        """groups — (название, метки, факты). Возвращает, сколько дописано."""
        jokes = self.book.jokes
        vocab = self.book.vocab
        ids, texts = {j.id for j in jokes}, {j.text.lower() for j in jokes}
        added = 0
        for start in range(0, len(groups), batch):
            part = groups[start:start + batch]
            blocks = []
            for name, tags, facts in part:
                have = [j.text for j in jokes if set(tags) & j.tags][:25]
                blocks.append(f"Группа «{name}», метки группы: {', '.join(tags)}.\nФакты: {facts}.\nУже есть:\n"
                              + ("\n".join(f"- {t}" for t in have) or "- ничего"))
            user = (f"Словарь меток: {', '.join(sorted(vocab))}.\n\nНапиши по {per_group} новых заготовок для "
                    f"каждой группы.\n\n" + "\n\n".join(blocks))
            result = self.complete(WRITER_SYSTEM, user, max_tokens=12000, model=self.model,
                                   title="Шутки: " + ", ".join(g[0] for g in part))
            if not result:
                report(f"Шутки: нет ответа для {[g[0] for g in part]} — пропускаю")
                continue
            lines_out: list[str] = []
            for group in result.get("groups") or []:
                name = str(group.get("group", "")).strip("«» ")
                base = next((tags for n, tags, _f in part if n == name), [])
                lines = []
                for j in group.get("jokes") or []:
                    text = str(j.get("text", "")).strip()
                    if not text or text.lower() in texts:
                        continue
                    tags = list(dict.fromkeys([*base, *(t for t in j.get("tags") or [] if t in vocab)]))
                    jid = "g-" + (re.sub(r"[^a-z0-9-]+", "-", str(j.get("id", "")).lower()).strip("-") or "joke")
                    base_id, n = jid, 2
                    while jid in ids:
                        jid, n = f"{base_id}-{n}", n + 1
                    ids.add(jid)
                    texts.add(text.lower())
                    entry = {"id": jid, "text": text, "src": "своя", "tags": tags, "new": True}
                    lines.append("  - {" + ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False)}"
                                                     for k, v in entry.items()) + "}")
                if lines:
                    lines_out.append(f"\n  # ---------------------------------------------------------------- "
                                     f"своя: {name} (new — просмотреть)\n" + "\n".join(lines))
                    report(f"Шутки: {name} +{len(lines)}")
                    added += len(lines)
            if lines_out:
                self._append(out, "".join(lines_out) + "\n")
        return added

    @staticmethod
    def _append(path: Path, text: str) -> None:
        """Дописать в конец, не трогая остальное; файл должен остаться читаемым (иначе — откатить)."""
        before = path.read_text(encoding="utf-8") if path.is_file() else AUTO_HEADER
        after = before + text
        yaml.safe_load(after)  # не YAML — исключение, файл не тронут
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(after)


class Jokes:
    """Шутки к моменту: метки обстановки + база + общая история + память о сказанном; пополнение в фоне."""

    def __init__(self, book: JokeBook, game: GameState, places: PlaceBook | None, memory: MemoryKeeper,
                 writer: JokeWriter | None = None, cfg: JokesConfig | None = None):
        self.book = book
        self.game = game
        self.places = places
        self.memory = memory
        self.writer = writer
        self.cfg = cfg or JokesConfig()
        self._last_refill = 0.0
        self._refilling = threading.Lock()

    # ------------------------------------------------------------ для мозга

    def block(self, changes: list[Change] | None = None, extra: set[str] | None = None) -> str:
        """Блок для запроса модели: заготовки к месту и строки общей истории. "" — нечего предложить."""
        tags = self.scene_tags(changes or []) | (extra or set())
        said, days = self._said()
        jokes = self.book.pick(tags, said, days)
        history = self.history(tags, said, days)
        self._maybe_refill(tags, said, days)
        if not jokes and not history:
            return ""
        out = ("Заготовки шуток к месту — местные байки и поговорки (можешь обыграть одну своими словами, коротко и "
               "к месту; тогда добавь в JSON \"joke\": \"<её id>\"; не к месту — не бери):\n"
               + "\n".join(f"- {j.id}: {j.text}" for j in jokes) + "\n") if jokes else ""
        if history:
            out += ("Из вашей общей истории — тоже можно по-доброму подколоть, если к месту (id так же, в \"joke\"):\n"
                    + "\n".join(f"- {hid}: {text}" for hid, text in history) + "\n")
        return out

    def said(self, joke_id: str) -> None:
        """Гослинг обыграл заготовку или строку истории — в память персонажа (по сейву)."""
        if not (joke_id.startswith("h-") or self.book.has(joke_id)):  # выдумал id или обыграл то, чего не давали
            log.info("Шутка: заготовки «%s» нет в базе — не запоминаем", joke_id)
        elif self.memory.store.is_open:
            self.memory.store.add_encounter(SAID, joke_id, self.memory.where())
            log.info("Шутка сказана: %s", joke_id)

    def _said(self) -> tuple[dict[str, tuple[float, float | None]], float | None]:
        if not self.memory.store.is_open:
            return {}, None
        return self.memory.store.last_by_key(SAID), self.memory.where().game_days

    # ------------------------------------------------------------ общая история

    def history(self, tags: set[str], said: dict[str, tuple[float, float | None]],
                days: float | None) -> list[tuple[str, str]]:
        """Строки из памяти: гибели Владоса в этой ветке сейва и привычки по статистике — к месту и не сказанные
        недавно. [(id, текст)], id — «h-…»."""
        now, out = time.time(), []

        def fresh(hid: str) -> bool:
            return hid not in said or not recently(said[hid], now, days)

        if tags & DEATH_TAGS and self.memory.store.is_open and fresh("h-deaths"):
            killers = Counter((m[1].strip() if (m := KILLER.search(t)) else "сам — упал, утонул или ловушка")
                              for t in self.memory.store.deaths())
            total = sum(killers.values())
            # Одна гибель — это сам повод died («Владос погиб…»); история — со второй.
            if total >= 2 or (total == 1 and "гибель" not in tags):
                who = ", ".join(k + (f" ×{c}" if c > 1 else "") for k, c in killers.most_common(4))
                out.append(("h-deaths", f"Владос погибал уже {times(total)} — {who}"))
        stats, habits = self.game.profile.stats, []
        for stat, least, when in HISTORY_STATS:
            value = stats.get(stat, 0)
            hid = "h-" + stat.lower().replace(" ", "-")
            if value >= least and tags & when and fresh(hid):
                habits.append((hid, f"по делам Владоса — {HABITS.get(stat, stat)}: {value}"))
        random.shuffle(habits)  # гибели — первыми, привычки — какая придётся
        return (out + habits)[:HISTORY_PICK]

    # ------------------------------------------------------------ пополнение

    def _maybe_refill(self, tags: set[str], said: dict[str, tuple[float, float | None]], days: float | None) -> None:
        """Несказанных заготовок к обстановке мало — дописать новые в фоне (не чаще jokes.refill_gap)."""
        if (not self.writer or not self.cfg.refill or not self.book.auto_path
                or time.time() - self._last_refill < self.cfg.refill_gap):
            return
        fresh = [j for j in self.book.eligible(tags, said, days) if j.id not in said]
        if len(fresh) >= self.cfg.refill_below:
            return
        # Под самые редкие метки обстановки: «Данстар, шахта», а не «бой, ночь». Без «кто такой Владос»: с меткой
        # «вор» (много краж) все шутки про Данстар вышли про кражи.
        key = sorted((t for t in tags if t in self.book.vocab and t not in CONDITIONS[1]),
                     key=lambda t: -self.book.weight(t))[:REFILL_KEY_TAGS]
        if not key or not self._refilling.acquire(blocking=False):
            return
        self._last_refill = time.time()
        threading.Thread(target=self._refill, args=(key,), daemon=True, name="jokes-refill").start()

    def _refill(self, key: list[str]) -> None:
        try:
            facts = "; ".join(f for _n, tags, f in TOPICS if set(tags) & set(key))
            location = self.game.status.location
            if location and self.places and (card := self.places.card(location)):
                facts += ("; " if facts else "") + "про это место по данным игры: " + card[:800]
            name = ", ".join(key)
            log.info("Шутки: к обстановке «%s» мало несказанных — дописываю в фоне", name)
            added = self.writer.write([(name, key, facts or "общеизвестное в Скайриме")], self.book.auto_path,
                                      per_group=self.cfg.refill_count, batch=1)
            log.info("Шутки: дописано %d в %s", added, self.book.auto_path.name)
        except Exception:  # noqa: BLE001 — фоновая задача: попробуем в другой раз
            log.exception("Шутки: не дописать")
        finally:
            self._refilling.release()

    # ------------------------------------------------------------ метки обстановки

    def scene_tags(self, changes: list[Change]) -> set[str]:
        known = self.book.tags
        tags: set[str] = set()
        for c in changes:
            tags |= EVENT_TAGS.get(c.kind, set())
            tags |= {tag for pattern, tag in TEXT_TAGS if pattern.search(c.text)}
        s = self.game.status
        if s.location == "":
            tags |= {"дикие земли", "дорога"}
        # Владение не ищем в справке: «Вайтран» — и владение, и город, а в Ветреном пике мы не в городе.
        for name in (s.location, s.cell):
            if not name:
                continue
            tags.add(name)
            place = self.places.find(name) if self.places else None
            if place:
                tags |= {KIND_TAGS.get(k, k) for k in place.kinds()}
                if (town := self.places.settlement_of(place)) and town.name:
                    tags.add(town.name)
        if s.hour is not None and (s.hour >= 22 or s.hour < 5):
            tags.add("ночь")
        weather = (s.weather or "").lower()
        if "снег" in weather or "метел" in weather or "буран" in weather:
            tags |= {"снег", "холод"}
        if "дожд" in weather or "гроз" in weather:
            tags.add("дождь")
        if s.in_combat:
            tags.add("бой")
        if s.health is not None and s.health < 0.3:
            tags.add("ранение")
        for n in (s.nearby or []) + (s.threats or []):
            who = f"{n.name} {race_name(n.race)}".lower()
            tags |= {tag for part, tag in NEARBY_TAGS if part in who}
            tags.add(n.name)
        for guild in self.game.profile.guilds():
            if tag := GUILD_TAGS.get(guild):
                tags.add(tag)
        stats = self.game.profile.stats
        tags |= {tag for name, least, tag in STAT_TAGS if stats.get(name, 0) >= least}
        bag = self.game.player.get("bag") or {}
        if sum(int(info.get("total", 0)) for cat, info in bag.items() if "оруж" in cat or "брон" in cat) >= OVERLOAD:
            tags.add("перегруз")
        return tags & (known | {"тишина"})
