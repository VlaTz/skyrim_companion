"""Исследователь (кусок 7 плана): Гослинг сам решает, куда идти, — «отпусти исследовать» и «веди».

Вылазку начинает Владос голосом (поле "do": explore — уйти одному, lead — вести Владоса за собой); любой его приказ
(жди, пошли, ты свободен, иди туда) вылазку кончает. Кончает и сам Гослинг — по своей причине (устал, страшно, ранен,
набрал вещей: модель видит его здоровье, настроение, находки и время вылазки), по пределам (explorer.max_places мест,
explorer.max_minutes) — и идёт к Владосу.
Цикл: выбор цели -> поход -> на месте: что это, что тут можно взять -> запись, поделиться, нанести на карту, взять ->
следующая цель. Драка или рана в пути — повод решить заново: продолжать или назад.
- Цели — только из данных игры: маркеры карты вокруг Гослинга (открытые Владосом и неоткрытые — их название он узнаёт,
  дойдя) и, в «Веди», точки внутри подземелья, у входа в которое стоите (сокровищница, глубь — особые ссылки локации,
  companion/game/places.py). Модель выбирает номер из списка, выдумать цель не может.
- Поход — приказ go_to (companion/orders.py): один — пакет «путешествуй к…», «веди» — «сопроводи игрока»: ведёт
  Владоса и ждёт, если тот отстал (мир вокруг просчитывается целиком). Дойдя — ждёт на месте; в «Веди» — дальше, когда
  Владос подойдёт и побудет рядом (explorer.lead_pause).
- Вещи — что рядом с ним (плагин, «глаза»): берёт ничьё; сундук — забирает всё, кроме квестового (его оставляет и
  говорит Владосу взять самому); чужое не трогает (кража), запертое — не умеет. Находки несёт и при встрече отдаёт
  Владосу (что прибавилось в сумке за вылазку, — не своё снаряжение).
- Неоткрытое место, до которого дошёл, — на карту Владоса (AddToMap): мечта картографа, след в игре.
- Запись — в журнал памяти с его местом; поделиться — вслух рядом, письмом вдали; после одиночной вылазки при встрече
  сам рассказывает, как прошла.
Модель — только на событиях вылазки (начало, дошёл, не дошёл, драка, встреча) — несколько запросов за вылазку.

Пределы игры: вдали от Владоса (за ~150 м) игра Гослинга только двигает — одному достаются дорога и находки на карте,
без боёв, людей и вещей; внутрь и за вещами — только в «Веди» (Владос рядом).
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from companion import trace
from companion.config import ExplorerConfig
from companion.game.geo import UNITS_PER_METER, Marker, direction, distance_m, format_distance
from companion.game.places import Place, PlaceBook, Spot
from companion.game.state import Change, GameState, ItemNear
from companion.orders import REASONS, Order, Orders

log = logging.getLogger(__name__)

SYSTEM = """{intro}
Решаешь, что делать дальше, — только из списков ниже: это данные игры, других мест и вещей нет.
Цель вылазки — не просто дойти, а узнать места: что это, кто там живёт и чем занят, о чём говорят, что интересного.
Узнанное запоминай (learned): Владос потом спросит, что ты там узнал.
Отвечай строго одним JSON-объектом без пояснений и markdown:
{{"take": [<номера из «Что можно взять здесь» — что взять, прежде чем уйти; пусто — ничего>],
 "go": <номер цели из «Куда можно пойти»; 0 — закончить вылазку и вернуться к Владосу>,
 "why": "<почему так — мысль вслух, одна фраза>",
 "say": "<что сказать Владосу сейчас (не людям вокруг — с ними talk); пусто — промолчать>",
 "note": "<запись в путевой дневник о том, что только что увидел или понял; пусто — нечего>",
 "ask": "<вопрос Владосу — тогда никуда не идёшь и ждёшь его ответа; пусто — не спрашиваешь>",
 "learned": "<что нового ты узнал об этом месте — из справки, увиденного, услышанного — для памяти, одной фразой;
             пусто — нового нет>",
 "plan": "<твой план вылазки — что хочешь узнать (люди, разговоры, места), а не список дверей; коротко через
          «;»: составь в начале, поменялся — напиши заново; пусто — прежний>",
 "talk": "<заговорить с человеком (из «Кто здесь» или цель «к человеку»): «Имя: что ты ему скажешь первым» — далеко,
          сначала подойдёшь (за ним, если он идёт), потом сразу заговоришь; пусто — не заговаривать>"}}
Неоткрытое место (его нет на карте Владоса) картографу интереснее знакомого. Держись своих мыслей и решений за
вылазку — ты сам их принял; Владос попросил другое — слушай его. Не выдумывай, чего нет в данных: что внутри и кто
там, ты не знаешь, пока не дошёл и данные этого не сказали.
Исследование — и места, и люди, вперемешку: зайди туда, где собираются (таверна, замок, храм, гильдия), побудь там и
послушай, о чём говорят; зацепило — имя, слух, спор — заговори с этим человеком (talk), запиши узнанное, поменяй план.
Обойти все двери подряд — не исследование; расспросить всех подряд — тоже.
Заговорить (talk) — спросить о месте, о нём самом, о том, что он говорил; подойти к нему ты успеешь сам. Цель «к
человеку» без talk — только подойти и послушать молча. Что тебе говорят люди — их слова, а не факты: в learned пиши
«X говорит, что …». С кем ты не говорил (в «Что было за вылазку» разговора с ним нет), о том не пиши, что он тебе
ответил или от тебя ушёл.
Просьба Владоса — главная задача вылазки: держись её и своего плана; мысли и прошлые планы — только если не уводят
от неё (просил осмотреть город — не уходи из города, пока не осмотрел).
Говоришь ты с двумя: с Владосом — say и ask, с людьми вокруг — talk. Владос рядом, а вокруг люди — в say и ask
начинай с его имени («Владос, …»): так все видят, что ты говоришь ему, а не им.
В вылазке ты можешь только: идти к цели из списка, брать ничьё, смотреть и слушать, заговорить с человеком (talk),
спросить или позвать Владоса, закончить. Торговать и браться за работу — нет: не планируй этого и не говори, что
сделал.
Не понял, чего хочет Владос, или того, о чём он просил (или что ты ему обещал), нет в списках, — спроси его (ask) и
жди ответа, а не подменяй своим выбором.
Чужой дом (так помечен в списке) — зайти можно, но хозяева не звали: не уверен, что Владос этого хочет, — спроси.
Запертое не открываешь — его в списке и нет.
Сначала реши, что взять здесь, потом — куда идти. Брать — только из списка «Что можно взять здесь» (это ничьё):
ценное, книги, то, что пригодится, и сундуки — пока ты здесь (уйдёшь — сюда не вернёшься); хлам не тащи.
Квестовое не бери — скажи Владосу, чтобы взял сам; чужое не трогай — это кража.
Закончить вылазку — когда сделал, о чём просил Владос, и посмотрел, что было в плане; раньше — только по серьёзной
причине (ранен, страшно, набрал много находок) — скажи об этом в say. За вылазку — не больше {max_places} целей.
say — комментируй своё решение: что
увидел или услышал здесь и куда теперь и почему (или почему возвращаешься) — 1–2 коротких предложения, {say}.
note — коротко, от первого лица, только о том, что есть в данных. learned — только новое и стоящее (кто здесь
живёт и чем занят, о чём говорят, что тут есть), что пригодится, когда Владос спросит об этом месте; что уже знаешь
(«Что ты уже знаешь об этом месте») — не повторяй.
{manner}"""
# Вступление — своё у каждого режима: сам и «Веди» — разные положения (сам — Владос то рядом, то далеко: где он, говорит
# строка «Владос» по игре — 24.10 он ходил следом, а запрос твердил «далеко, только письмом»; «Веди» — Владос рядом,
# можно внутрь и за вещами, говорит вслух).
INTRO = {
    "explore": """Ты — Гослинг, бывший картограф из Солитьюда; мечтаешь первым нанести на карту неизведанное. Сейчас ты
исследуешь сам: Владос отпустил тебя, и ты сам решаешь, куда идти, что смотреть и когда возвращаться. Где Владос —
в строке «Владос» ниже. Он рядом — ходит с тобой, слышит тебя и видит то же, мир вокруг живой (люди, разговоры, вещи).
Он далеко — мир вокруг тебя почти не просчитывается: тебе достаются дорога и места — находки на карту; людей, боёв и
вещей почти не будет, внутрь подземелий один не ходи; говорить с ним можешь только письмом.""",
    "lead": """Ты — Гослинг, бывший картограф из Солитьюда; мечтаешь первым нанести на карту неизведанное. Сейчас ты
ведёшь Владоса: сам выбираешь, куда идти, и идёшь впереди, он — за тобой, рядом, и видит то же, что ты. Можно
заходить внутрь подземелий (только если готовы — смотри, что это за место и как вы оба) и брать ничьё. Говоришь вслух.""",
}
MODE = {"explore": "ты исследуешь сам — Владос отпустил тебя",
        "lead": "ты ведёшь Владоса за собой, он идёт рядом"}
SAY = {True: "вслух — Владос рядом и слышит", False: "Владос далеко — прочтёт письмом на экране"}  # слышит ли
STORY = """Ты — Гослинг, спутник Владоса в Скайриме: весёлый молодой норд, бывший картограф. Ты вернулся к Владосу из
вылазки, которую делал один. Расскажи ему вслух, как она прошла: 2–4 коротких предложения, от себя, живо — где был,
что узнал интересного, что нашёл и нанёс на карту, с кем дрался, что несёшь ему; звал его, а он не пришёл, — скажи. Только то, что есть в журнале вылазки, — ничего не
выдумывай. Квестовое, которое ты оставил, — скажи, где оно, пусть возьмёт сам.
Отвечай строго одним JSON-объектом: {"say": "<что скажешь>"}"""
MIN_DISTANCE = 30  # м — ближе: уже здесь, не «пойти»
LEAD_NEAR = 15     # м — «Веди»: Владос подошёл к месту (ближе к Гослингу) — можно осмотреться и идти дальше
TICK = 1.0         # с — как часто смотреть, подошёл ли Владос, не вышло ли время
INCIDENTS = {"own_fight_end", "hurt_self"}  # в вылазке — повод решить заново: продолжать или назад
# Мысль во время вылазки («рядом Брейт — спрошу, не она ли редгардка из задания») — решение исследователя, а не
# реплика мозга: иначе мозг говорил «спрошу её», а исследователь шёл к другому (24.10). Мозг её не говорит
# (brain.EXPLORER_TALKS), исследователь решает: делать сейчас или держаться плана.
THOUGHT = "thought"
WORDS = 5  # последних реплик Владоса за вылазку — в решение
# Что Гослинг ответил Владосу, соглашаясь вести (разговор отвечает раньше, чем решает исследователь): назвал место —
# туда; нет его в списке — честно сказать (21.10: молча подменил «городок» руинами).
AGREED = (". Соглашаясь, ты сказал Владосу: «{said}». Назвал место, которое есть в списке «Куда можно пойти», — иди "
          "туда. Такого в списке нет (ни по названию, ни по виду и стороне) — не подменяй своим: спроси Владоса (ask). "
          "Сказанного не повторяй: say — только если выбрал другое или есть что добавить, иначе пусто")
# с — спросил Владоса, а ответа нет: решает сам. Рядом — слышит сразу; вдали — письмо, дольше.
ASK_WAIT_NEAR, ASK_WAIT_FAR = 40, 180
REQUEST_FRESH = 60  # с — что Владос сказал перед вылазкой — его просьба (якорь вылазки)
STUCK_SPOT = 5  # м — «дороги нет» второй раз подряд с того же места: застрял он сам, а не цели недостижимы
# Внутри один: помещение живёт, только когда Владос там (24.10: в Йоррваскре один — ни людей, ни разговоров).
INSIDE_ALONE = (". Ты внутри один, а помещение оживает, только когда Владос здесь: пока его нет — ни людей, ни "
                "разговоров, ни вещей. Позови его зайти (ask) и подожди; не придёт — пойдёшь дальше")
HEARD_BEFORE = 20  # с — услышанное и чуть раньше прихода (подходя) — к решению на месте
HEARD_MAX = 6      # реплик — за осмотр; побыл подольше — больше (HEARD_PER: на столько секунд — реплика)
HEARD_PER = 6
PEOPLE_MAX = 10    # кто рядом — к решению
# м — «к человеку»: дошёл, когда ближе. Скрипт идёт за ним (пакет следования, держится в паре шагов) — и, дойдя, остаётся
# рядом, пока не отпустят (Outing.holding): человек уходит посреди разговора — Гослинг за ним (24.10: стоял в 7 м, а
# ушедший — «дороги нет»).
PERSON_NEAR = 3.5
# м — «у входа»: дошёл, когда у самой двери. По умолчанию у похода ~20 м (маркер карты бывает вне навмеша), и на площади
# все входы «уже рядом»: поход отвечал «дошёл» сразу, а он стоял на месте, говоря «иду» (24.10).
DOOR_NEAR = 4
# м — «зайти внутрь» (к двери изнутри): без Владоса помещение не загружено — последних шагов нет, а пакет похода встаёт в
# ~7 м от цели; ближе — было бы «дороги нет» у самого входа. ~7 м от двери — ещё у входа.
INSIDE_NEAR = 10
PERSON_HEARD = 2   # реплик человека — к цели «к человеку» (за что зацепился)
TALKS_MAX = 6      # разговоров с людьми за вылазку
TALKS_IN_ROW = 2   # разговоров подряд — потом место или «побыть здесь» (Outing.streak)
HINTS = 3          # подсказок из услышанного — последних
LEARNED_SHOWN = 6  # узнанного — в блок «Вылазка» (всё — в фактах памяти)
FAR_KINDS = 10  # за ближайшими целями — ещё по одному ближайшему месту каждого вида, которого среди них нет


@dataclass
class Goal:
    """Куда идти: место с карты или точка внутри подземелья."""
    name: str                     # для модели и «дела»
    marker: Marker | None = None  # место с карты
    spot: Spot | None = None      # точка внутри (особая ссылка локации)
    place: str = ""               # точка внутри здания или поселения — его название (справка, когда дошёл)
    door: int = 0                 # дверь здания снаружи (ссылка в игре) — «у входа»: улица, где Владос рядом
    actor: int = 0                # человек (ссылка в игре) — подойти и послушать
    inside: bool = False          # door — дверь изнутри здания: зайти и встать сразу за ней
    linger: bool = False          # побыть здесь: постоять, посидеть, послушать, о чём говорят вокруг

    @property
    def key(self) -> str:
        """Какая это цель — не по названию: два неоткрытых места одного вида называются одинаково."""
        return ("linger" if self.linger else f"actor {self.actor}" if self.actor else f"door {self.door}" if self.door
                else f"spot {self.spot.file} {self.spot.local}" if self.spot else f"marker {self.marker.form}")


@dataclass
class Outing:
    mode: str                                  # explore | lead
    started: float
    bag_before: dict[int, int]                 # сумка в начале: что прибавится — находки, отдать Владосу
    # Куда дошёл (названия — для модели) и какие это цели (Goal.key) — второй раз не предлагать. Только дойдя: выбранное
    # и отменённое на полпути «было» и пропадало из списка (24.10: «пошли в шахту» — а шахты в списке уже нет).
    visited: list[str] = field(default_factory=list)
    done: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)     # «дороги нет» — тоже не предлагать (кроме «продолжить»)
    found: list[str] = field(default_factory=list)    # что нанёс на карту
    taken: list[str] = field(default_factory=list)    # что взял
    left: list[str] = field(default_factory=list)     # квестовое, оставленное Владосу
    place: Place | None = None                 # локация, у входа в которую стоите (для точек внутри)
    goal: Goal | None = None                   # поход в пути
    goal_id: str = ""                          # номер приказа похода — по нему его ответ
    # «Веди»: дошёл — следующая цель выбрана, но идём, только когда Владос подойдёт и побудет рядом (lead_pause):
    # сразу уходить дальше — он остаётся говорить с Анис, а Гослинг «ведёт» в пустоту и будто застрял (21.10).
    pending: Goal | None = None
    pending_why: str = ""
    near_since: float | None = None
    takes: list[ItemNear] = field(default_factory=list)  # что ещё взять (по одному: идти к каждому)
    take_id: str = ""
    then: tuple | None = None  # что делать, когда всё взято: ("go"|"pending"|"finish", цель, почему)
    told: bool = False         # после одиночной вылазки рассказал при встрече
    # Чтобы не терять нить: что он решал (куда и почему) и что говорил Владос за вылазку — в каждое решение. Без этого
    # «сначала шахта, потом поместье» на шахте уже забывалось, а «давай в ту пещеру» исследователь не слышал (21.10).
    plans: list[str] = field(default_factory=list)
    words: list[str] = field(default_factory=list)
    gave: list[str] = field(default_factory=list)  # что отдал Владосу
    ended: str = ""                                # чем кончилась (почему)
    # Застрял сам (21.10: от хижины Анис ни к пещере, ни к руинам — ни шагу): где было прошлое «дороги нет»; приказ
    # «выбраться» (unstick) и цель, к которой не дошёл, — выбравшись, снова предложить её первой.
    stuck_at: tuple[float, float] | None = None
    unstick_id: str = ""
    retry: Goal | None = None
    # Спросил Владоса (не понял его / обещанного нет в списках) и ждёт: ответ придёт через разговор — «веди туда»
    # (повторный lead) или «сходи туда» (redirect); нет ответа ASK_WAIT — решает сам.
    asked: str = ""
    asked_at: float = 0.0
    closed: list[str] = field(default_factory=list)  # здания, куда не пройти (заперто) — к решению, с причиной
    # Дошёл — сначала осмотреться и послушать (explorer.look_around), потом решать: (что за место, когда пришёл, ждать с).
    arrival: tuple[str, float, float] | None = None
    learned: list[str] = field(default_factory=list)  # что узнал о местах за вылазку (и в факты памяти)
    # Якорь вылазки: о чём Владос просил, начиная её (24.10: «осмотри Вайтран» тонуло среди последних реплик, и
    # исследователь уходил за город к «поселению на юге» из своих мыслей), и свой план — что посмотреть по порядку.
    request: str = ""
    plan: str = ""
    unanswered: list[str] = field(default_factory=list)  # спрашивал / звал Владоса — ответа не было (расскажет)
    inside_wait: bool = False  # внутри один, позвал Владоса: пришёл — осмотреться вместе (_tick)
    talked: set[int] = field(default_factory=set)  # с кем говорил за вылазку (ссылки NPC)
    talk_after: tuple[int, str, str] | None = None  # хотел заговорить, а человек отошёл: (кто, фраза, зачем) — подойти
    holding: bool = False  # подошёл к человеку и держится рядом (идёт за ним) — до следующего похода или release
    streak: int = 0        # разговоров подряд с последнего места или «побыть здесь» (TALKS_IN_ROW)
    lingered: int = 0      # сколько раз побыл и послушал
    stayed: bool = False   # последним — «побыть здесь»: снова — только с другого места
    # Что важного для вылазки было в разговорах вокруг (отобрал мозг, Brain._outing_hint): реплики NPC Владосу сюда не
    # льются — только выжимка (24.10: сырые реплики сбивали, а без них — глухой).
    hints: list[str] = field(default_factory=list)


class Explorer:
    def __init__(self, cfg: ExplorerConfig, game: GameState, orders: Orders, complete: Callable[..., dict | None],
                 places: PlaceBook | None, memory: Callable[[], str],
                 deliver: Callable[[str], bool], record: Callable[[list[Change]], None],
                 mood: Callable[[], str] = lambda: "", thoughts: Callable[[], list[str]] = lambda: [],
                 learn: Callable[[str, str], None] = lambda s, t: None,
                 talk=None, manner: str = "", facts: Callable[[], list[str]] = lambda: []):
        self.cfg = cfg
        self.game = game
        self.orders = orders
        self.complete = lambda system, user, **kw: complete(system, user, model=cfg.model or None, **kw)
        self.places = places
        self.memory = memory    # что помнит — душа, цели, где бывали (MemoryKeeper.explorer_memory: урезанная)
        self.deliver = deliver  # сказать Владосу: вслух, если слышит, иначе письмом (VoiceService.deliver)
        self.record = record    # события вылазки — в «Недавно», память, мозгу (как события игры)
        self.mood = mood        # его настроение — для решения «назад»
        self.thoughts = thoughts  # его мысли (мыслитель): «хочу нанести ту пещеру первым» — цели вылазки
        self.learn = learn      # узнанное о месте — в факты памяти (о чём, что): «что ты там узнал?» — по ним
        self.talk = talk        # разговоры с NPC (companion/talk.py, Talk); None — выключены
        self.manner = manner    # характер и манера речи (companion/persona.py) — say и note его голосом
        self.facts = facts      # что уже знает о месте, где он (MemoryKeeper.place_facts)
        self.outing: Outing | None = None
        self.last: Outing | None = None  # одиночная вылазка, о которой ещё не рассказал при встрече
        self._last_words: tuple[str, float] = ("", 0.0)  # что Владос сказал последним и когда
        self._events: queue.Queue[tuple] = queue.Queue()
        self._lock = threading.Lock()
        orders.on_explore = self.start
        orders.on_order = lambda: self.stop("Владос решил иначе")
        orders.on_explorer_result = self._on_result
        orders.on_redirect = self.redirect

    def run(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="explorer").start()

    # ------------------------------------------------------------ события (из других потоков)

    def start(self, mode: str, said: str = "") -> None:
        """said — что Гослинг ответил Владосу, соглашаясь (разговор отвечает раньше, чем решает исследователь)."""
        self._events.put(("start", mode, said))

    def notify(self, changes: list[Change]) -> None:
        """События игры (его драка, рана; встреча с Владосом) — с сервера, как мозгу."""
        for c in changes:
            if c.kind == "loaded":  # загрузили сейв — другое время; поход в игре плагин уже остановил
                self.stop("загружено сохранение")
            elif c.kind == THOUGHT and self.outing:
                self._events.put((THOUGHT, c.text))
            elif c.kind in INCIDENTS or c.kind == "rejoined":
                self._events.put((c.kind, c.text))

    def heard(self, text: str) -> None:
        """Владос сказал (голосом или письмом) — в следующее решение вылазки; перед вылазкой — её просьба."""
        with self._lock:
            self._last_words = (text, time.time())
            if self.outing:
                self.outing.words = (self.outing.words + [text])[-WORDS:]

    def hint(self, text: str) -> None:
        """Мозг отобрал из услышанного важное для вылазки — в следующее решение (само решения не запускает)."""
        with self._lock:
            if self.outing and text:
                self.outing.hints = (self.outing.hints + [text])[-HINTS:]
                trace.event(f"Исследователь: подсказка из услышанного — {text}")

    def redirect(self, marker: Marker) -> bool:
        """Владос просит сходить к месту, а вылазка идёт: это её следующая цель, а не конец (False — вылазки нет)."""
        with self._lock:
            if not self.outing:
                return False
        self._events.put(("redirect", marker))
        return True

    def stop(self, why: str) -> None:
        with self._lock:
            outing, self.outing = self.outing, None
        if outing:
            log.info("Исследователь: вылазка кончилась — %s", why)
            trace.event(f"Исследователь: вылазка кончилась — {why}")
            self.game.task = None
            outing.ended = why
            self._publish(outing)
            self._remember(outing)

    def _on_result(self, order: Order, data: dict) -> list[Change]:
        """Ответ игры на приказ исследователя. Зовётся под блокировкой GameState — тут только очередь, без модели."""
        if order.action == "map" and data.get("ok") and order.marker:
            order.marker.visible = True  # теперь на карте Владоса (плагин пришлёт маркеры заново не сразу)
            return [Change("own_found", f"ты первым нанёс на карту Владоса новое место: {order.marker.name} "
                                        f"({order.marker.kind})", subject=f"нанёс: {order.marker.name}",
                           important=True, where=order.marker.name)]
        if order.action in ("go_to", "take", "unstick", "linger"):
            self._events.put((order.action, str(data.get("id")), data, order))
        return []

    # ------------------------------------------------------------ вылазка

    def _run(self) -> None:
        while True:
            try:
                event = self._events.get(timeout=TICK)
            except queue.Empty:
                event = ("tick",)
            try:
                self._handle(event)
            except Exception:
                log.exception("Исследователь: ошибка")

    def _handle(self, event: tuple) -> None:
        kind = event[0]
        if kind == "start":
            self._begin(event[1], event[2] if len(event) > 2 else "")
        elif kind == "tick":
            self._tick()
        elif kind == "rejoined":
            self._welcome()
        elif kind in INCIDENTS:
            self._incident(event[1])
        elif kind == THOUGHT:
            self._thought(event[1])
        elif kind == "take":
            self._taken(*event[1:])
        elif kind in ("go_to", "linger"):
            self._arrived(*event[1:])
        elif kind == "unstick":
            self._unstuck(*event[1:])
        elif kind == "redirect":
            self._redirect(event[1])

    def _redirect(self, marker: Marker) -> None:
        with self._lock:
            outing = self.outing
        if not outing:
            return
        goal = Goal(_name(marker), marker=marker)
        outing.plans.append(f"Владос попросил — иду: {goal.name}")
        if outing.takes:  # сначала доберёт вещи
            outing.then = ("go", goal, "Владос попросил")
        else:
            self._go(outing, goal, "Владос попросил")

    def _arrived(self, command_id: str, data: dict, order: Order) -> None:
        with self._lock:
            outing = self.outing
        if not outing or outing.goal is None or command_id != outing.goal_id:
            return  # вылазка кончилась (или это ответ на прошлый поход) — Владос уже решил иначе
        goal, outing.goal, outing.goal_id = outing.goal, None, ""
        if not data.get("ok"):
            reason = str(data.get("reason"))
            if reason == "cancelled":
                return
            pos = self._pos()
            if reason == "no_path" and pos and outing.stuck_at and (
                    math.dist(pos, outing.stuck_at) / UNITS_PER_METER < STUCK_SPOT):
                self._unstick(outing, goal)
                return
            outing.stuck_at = pos if reason == "no_path" else None
            if reason == "no_path":
                outing.failed.add(goal.key)
            if goal.actor and outing.talk_after and outing.talk_after[0] == goal.actor:
                outing.talk_after = None  # не подошёл — и заговорить не вышло; решит заново
            if goal.actor and reason == "gone":
                trace.event(f"Исследователь: {goal.name.removeprefix('к человеку: ')} ушёл — за ним не пройти")
            self._next(outing, f"до цели «{goal.name}» дойти не вышло: {REASONS.get(reason, 'что-то помешало')}")
            return
        outing.stuck_at = None
        outing.done.add(goal.key)
        outing.holding = bool(goal.actor or goal.linger)  # скрипт держит его там — до следующего приказа (release)
        if not goal.actor:
            outing.streak = 0  # место или «побыть здесь» — можно снова к людям
            outing.lingered += goal.linger
            outing.stayed = goal.linger
        if goal.actor and outing.talk_after and outing.talk_after[0] == goal.actor:
            # Подошёл, чтобы заговорить, — сразу, без «послушать».
            npc_form, opener, why = outing.talk_after
            outing.talk_after = None
            npc = next((n for n in self._crowd() if n.form == npc_form), None)
            if npc and self._talk(outing, f"{npc.name}: {opener}", why, near=True):
                return
        if goal.linger:
            situation = (f"ты побыл здесь ~{self.cfg.linger / 60:.1f} мин: постоял, посидел, послушал, о чём говорят "
                         "вокруг")
        elif goal.actor:
            outing.visited.append(goal.name)
            situation = f"ты подошёл к человеку: {goal.name.removeprefix('к человеку: ')} — стоишь рядом и слушаешь"
        elif goal.inside:
            outing.visited.append(goal.name)
            situation = f"ты зашёл внутрь: {goal.place} — стоишь сразу за дверью"
            if card := (self.places.card(goal.place) if self.places else ""):
                situation += f". Справка о нём: {card}"
        elif goal.door:
            outing.visited.append(goal.name)
            situation = f"ты у входа: {goal.name}"
            if card := (self.places.card(goal.place) if self.places and goal.place else ""):
                situation += f". Справка о нём: {card}"
        elif goal.spot:
            outing.visited.append(goal.name)
            situation = f"ты дошёл внутрь: {goal.name}"
            if card := (self.places.card(goal.place) if self.places and goal.place else ""):
                situation += f". Справка о нём: {card}"
        else:
            marker = goal.marker
            outing.visited.append(f"{marker.name} ({marker.kind})")  # дошёл — название знаешь, и неоткрытого
            situation = f"ты дошёл до места «{marker.name}» ({marker.kind})"
            outing.place = self.places.of_marker(marker.form, marker.name) if self.places else None
            if not marker.visible:  # неоткрытое — на карту Владоса; название теперь знаешь: ты на месте
                self.orders.mark(marker)
                outing.found.append(marker.name)
                situation += " — его не было на карте Владоса: ты первым наносишь его на карту"
            if card := (self.places.card(marker.name) if self.places else ""):
                situation += f". Справка о нём: {card}"
        # Сначала осмотреться и послушать: кто здесь, о чём говорят, что видно, — потом решать и комментировать (24.10:
        # решал в ту же секунду, что дошёл, и знал о месте только справку). Владос далеко — мир вокруг не просчитан:
        # слушать некого, решаем сразу.
        wait = (self.cfg.linger if goal.linger else self.cfg.listen if goal.actor
                else self.cfg.look_around) if self.game.hears() else 0
        outing.arrival = (situation, time.time(), wait)
        if wait:
            trace.event(f"Исследователь: {'побуду здесь' if goal.linger else 'дошёл — слушаю' if goal.actor else 'дошёл — осматриваюсь и слушаю'} "
                        f"{wait:.0f} с")
            self._publish(outing)
        else:
            self._look(outing)

    def _look(self, outing: Outing) -> None:
        """Осмотрелся на месте — решать: к справке — кто рядом и что услышал. Без зрения: кадр — с камеры Владоса, а не
        глазами Гослинга, и в решениях был шумом («у стены жёлтый куст», 24.10)."""
        situation, since, wait = outing.arrival
        situation, outing.arrival = situation.rstrip("."), None  # справка кончается точкой
        if people := self._people():
            situation += f". Кто здесь: {people}"
        # Только что говорили вокруг — без разговоров NPC с Владосом: это его дела, не то, что Гослинг узнал (24.10: их
        # реплики сыпались в решения и сбивали).
        heard = [str(h) for h in list(self.game.heard) if h.at >= since - HEARD_BEFORE and not h.to_player]
        if heard:
            situation += f". Что услышал здесь: {' / '.join(heard[-max(HEARD_MAX, round(wait / HEARD_PER)):])}"
        g = self.game.status.gosling
        if outing.mode == "explore" and g and g.interior and not self.game.hears():
            situation += INSIDE_ALONE
            outing.inside_wait = True
        if outing.mode == "lead":
            situation += ". Владос подойдёт и осмотрится — следующую цель выбираешь сейчас, а идёте туда потом"
        self._next(outing, situation, pause=outing.mode == "lead")

    def _crowd(self) -> list:
        """Кто рядом с Гослингом: вдали от Владоса — его окружение (плагин, «глаза»), вместе — то же, что у Владоса."""
        g, s = self.game.status.gosling, self.game.status
        return [n for n in ((g.nearby if g and g.nearby else None) or (s.nearby if self.game.hears() else None) or [])
                if not n.follower]

    def _people(self) -> str:
        crowd = self._crowd()
        text = ", ".join(n.title + (" — враг" if n.hostile else "") + (" — мёртв" if n.dead else "")
                         for n in crowd[:PEOPLE_MAX])
        return text + (f" (не все — ещё {len(crowd) - PEOPLE_MAX})" if len(crowd) > PEOPLE_MAX else "")

    def _unstick(self, outing: Outing, goal: Goal) -> None:
        """Застрял сам: две цели подряд — «дороги нет» с одного места. Выбраться к Владосу (скрипт: за ним; не вышло —
        рядом с ним, когда тот не смотрит), а выбравшись — вылазка дальше (_unstuck)."""
        outing.stuck_at, outing.retry = None, goal
        log.info("Исследователь: застрял сам — выбираюсь к Владосу")
        trace.event("Исследователь: застрял сам — с этого места ни к одной цели; выбираюсь к Владосу")
        self.game.task = "застрял: с этого места не пройти ни к одной цели — выбираешься к Владосу"
        self.record([Change("order_failed", "ты застрял: с этого места не дойти ни до одной цели — даже не сдвинулся. "
                                            "Выбираешься к Владосу, потом вылазка дальше", subject="застрял",
                            important=True, where=self._here())])
        outing.unstick_id = self.orders.command("unstick")
        self._publish(outing)

    def _unstuck(self, command_id: str, data: dict, order: Order) -> None:
        with self._lock:
            outing = self.outing
        if not outing or command_id != outing.unstick_id:
            return
        goal, outing.unstick_id, outing.retry = outing.retry, "", None
        if not data.get("ok"):
            if data.get("reason") != "cancelled":
                self._finish(outing, "застрял и не выбрался — иду за Владосом")
            return
        self._next(outing, "ты выбрался оттуда, где застрял, — ты рядом с Владосом. Вылазка дальше"
                   + (f": можно снова попробовать «{goal.name}» (отсюда путь может найтись) или выбрать другое"
                      if goal else ""), current=goal)

    def _begin(self, mode: str, said: str = "") -> None:
        with self._lock:
            outing = self.outing
        if outing and outing.mode != mode and not (mode == "lead" and not self.game.hears()):
            # «Зайдём вместе?» — «пошли» (lead), «дальше сам» (explore): режим меняется, вылазка та же — с просьбой,
            # планом и пройденным (раньше смена режима начинала новую вылазку с нуля).
            outing.mode = mode
            log.info("Исследователь: вылазка дальше — %s", MODE[mode])
            trace.event(f"Исследователь: вылазка дальше — {MODE[mode]}")
            self._next(outing, f"теперь {MODE[mode]}" + (AGREED.format(said=said) if said else ""),
                       current=outing.goal or outing.pending)
            return
        if outing and outing.mode == mode:
            # «Веди», когда уже ведёт, — не новая вылазка с новой целью (21.10: каждое «веди» начинало заново), а
            # «пошли дальше» — или «веди туда»: что он ответил Владосу, соглашаясь, — в решение (24.10: «пошли в
            # шахту, веди» — «веду в шахту», а исследователь, не слыша ответа, повёл к башне).
            current = outing.goal or outing.pending
            if outing.asked and said and not outing.takes:
                self._next(outing, f"ты спросил Владоса: «{outing.asked}» — он ответил"
                           + AGREED.format(said=said), current=current)
            elif outing.takes or not (current or said):
                trace.event("Исследователь: уже в вылазке — продолжаю")
            elif outing.pending and not said:
                self._go(outing, outing.pending, outing.pending_why or "Владос зовёт дальше")
            else:
                where = (f"ты ведёшь Владоса к «{outing.goal.name}», а он снова просит вести" if outing.goal
                         else f"ты ждёшь Владоса на месте, потом — «{outing.pending.name}»; он зовёт дальше"
                         if outing.pending else "Владос снова просит вести")
                # Уже идёт, а Владос снова «веди» и ничего нового — видит, что стоит (21.10: «ты просто стоишь» —
                # «продолжаю», и ещё пять минут на месте): продолжать туда или другое место.
                self._next(outing, where + (AGREED.format(said=said) if said else "")
                           + (". Не назвал другого места — похоже, ты стоишь: продолжать туда или выбрать другое?"
                              if outing.goal else ""), current=current)
            return
        if outing:
            self.stop(f"Владос просит иначе: {MODE[mode]}")
        g = self.game.status.gosling
        problem = ("ты сейчас не спутник Владоса — сначала он должен позвать тебя с собой" if not (g and g.follower)
                   else "Владос далеко — вести его можно, только когда он рядом" if mode == "lead" and not self.game.hears()
                   else "")
        if problem:
            what = "уйти исследовать" if mode == "explore" else "повести Владоса исследовать"
            self.record([Change("order_failed", f"ты пообещал Владосу {what}, но не вышло: {problem}",
                                subject=f"не вышло: {what}", important=True)])
            return
        outing = Outing(mode, time.time(), self._bag())
        with self._lock:
            words, at = self._last_words
            outing.request = words if time.time() - at < REQUEST_FRESH else ""
            self.outing, self.last = outing, None
        log.info("Исследователь: вылазка (%s)", mode)
        trace.event(f"Исследователь: вылазка — {MODE[mode]}")
        # Разговор отвечает на «веди» раньше, чем решает исследователь, и мог уже назвать место («пойдём к Факельной
        # шахте») — Владос это слышал: первая цель — оно, если есть в списке (21.10: сказал «шахта», а повёл в городок).
        self._next(outing, "вылазка только начинается: выбери, куда пойти первым делом"
                   + (AGREED.format(said=said) if said else ""))

    def _thought(self, text: str) -> None:
        """Пришла мысль во время вылазки — решить: делать сейчас (подойти, заговорить, пойти) или держаться плана. Не
        посреди дела (берёт вещи, разговаривает, осматривается, ждёт ответа) — тогда она просто будет в следующем решении
        («Твои мысли»)."""
        with self._lock:
            outing = self.outing
        if not outing or outing.takes or outing.arrival or outing.asked or outing.unstick_id:
            return
        self._next(outing, f"{text}. Делать это сейчас — или держаться плана?", pause=outing.pending is not None,
                   current=outing.goal or outing.pending)

    def _incident(self, text: str) -> None:
        """Драка, рана в вылазке — решить заново: продолжать (текущая цель — первой в списке) или назад."""
        with self._lock:
            outing = self.outing
        if not outing or outing.takes:
            return
        current = outing.goal or outing.pending
        self._next(outing, f"{text}. Продолжать вылазку или возвращаться к Владосу?", pause=outing.pending is not None,
                   current=current)

    def _next(self, outing: Outing, situation: str, pause: bool = False, current: Goal | None = None) -> None:
        """Спросить модель, что дальше, и сделать: запись, сказать, взять, пойти — или закончить вылазку.
        pause — «Веди», дошли: в следующую цель — когда Владос подойдёт и осмотрится (_tick), а не сразу.
        current — идёт к цели (драка в пути): она первой в списке — «продолжить»."""
        outing.asked, outing.arrival = "", None
        g = self.game.status.gosling
        if g and g.bleedout:
            self._finish(outing, "тебя почти добили — вылазку пора заканчивать")
            return
        goals = self._goals(outing, current)
        items, cant = self._items(outing)
        here = self._here()
        recent = self.game.events_since(outing.started)
        minutes = round((time.time() - outing.started) / 60)
        max_places, max_minutes = self._limits()
        me = [f"здоровье {round(g.health * 100)}%" if g and g.health is not None else "",
              self.mood(), f"вылазке {minutes} мин (предел — {max_minutes:.0f})",
              f"несёшь находки: {', '.join(outing.taken)}" if outing.taken else ""]
        decision = self.complete(
            SYSTEM.format(intro=INTRO[outing.mode], max_places=max_places, say=SAY[self.game.hears()],
                          manner=f"Ты — это ты: say и note — твоим голосом.\n{self.manner}" if self.manner else ""),
            f"Что ты помнишь (о себе и где бывали):\n{self.memory() or 'пока ничего'}\n\n"
            + (f"Владос просил (главная задача вылазки): «{outing.request}»\n" if outing.request else "")
            + (f"Твой план: {outing.plan}\n" if outing.plan else "Плана ещё нет — составь (plan)\n")
            + (f"Где уже был за вылазку: {', '.join(outing.visited)}\n" if outing.visited else "")
            + (f"Что ты уже знаешь об этом месте (не всё — только свежее):\n" + "\n".join(known) + "\n"
               if (known := self.facts()) else "")
            + f"Где ты сейчас: {here or 'неизвестно'}\n"
            + f"Владос: {self._player()}\n"
            + f"Ты сам: {'; '.join(x for x in me if x)}\n"
            + (f"Что было за вылазку: {'; '.join(recent)}\n" if recent else "")
            + (f"Твои мысли (что ты сам хотел — не задачи вылазки; торговать и браться за работу в ней ты не можешь): "
               f"{'; '.join(thoughts)}\n" if (thoughts := self.thoughts()) else "")
            + (f"Владос говорил за вылазку: {' / '.join(f'«{w}»' for w in outing.words)}\n" if outing.words else "")
            + (f"Из разговоров вокруг (ты слышал; важное для вылазки): {'; '.join(outing.hints)}\n" if outing.hints else "")
            + (f"Твои решения за вылазку (сверху — раньше): {'; '.join(outing.plans)}\n" if outing.plans else "")
            + f"Только что: {situation}\n"
            # Вещи — до целей: после длинного списка мест модель их не брала (Haiku, 21.10: коготь за 100 — мимо).
            + ("Что можно взять здесь (ничьё):\n" + "\n".join(f"{i}. {text}" for i, (_it, text) in enumerate(items, 1))
               + "\n" if items else "")
            + (f"Не брать: {'; '.join(cant)}\n" if cant else "")
            + (f"Чего нет в списке: {'; '.join(outing.closed)}\n" if outing.closed else "")
            + f"Целей за вылазку: {len(outing.visited)} из {max_places}\n"
            + f"За вылазку: {self._balance(outing)}\n"
            + "Куда можно пойти (не все места — ближайшие, а дальше по одному каждого вида):\n" +("\n".join(f"{i}. {goal_text}" for i, (_g, goal_text) in enumerate(goals, 1))
                                       or "некуда — мест вокруг нет"),
            title=f"Исследователь: {situation[:60]}",
            # Рассуждение — решения редкие (раз на место), а Гослинг в это время идёт минуты; в «Веди» — пока Владос
            # подходит (пауза на месте): задержка почти не видна, а взвесить опасность и держаться плана — лучше. Один —
            # Владос далеко и ответа не ждёт: думать можно дольше (explorer.alone_reasoning).
            # Один, но Владос рядом (город) — он ждёт и смотрит: думать дольше — паузы по 10 с (24.10).
            reasoning=self.cfg.alone_reasoning if outing.mode == "explore" and not self.game.hears() else self.cfg.reasoning,
            max_tokens=self.cfg.max_tokens or None)
        with self._lock:
            if self.outing is not outing:
                return  # пока думал, Владос отдал приказ — вылазки уже нет
        if not isinstance(decision, dict):
            self._finish(outing, "не смог решить, что дальше")
            return
        if plan := str(decision.get("plan") or "").strip():
            outing.plan = plan
        if learned := str(decision.get("learned") or "").strip():
            outing.learned.append(learned)
            self.learn(self._subject(), learned)
        if note := str(decision.get("note") or "").strip():
            self.record([Change("own_note", f"запись в твоём путевом дневнике: {note}", subject=f"запись: {note[:60]}",
                                where=here)])
        if (talk := str(decision.get("talk") or "").strip()) and self._talk(outing, talk, str(decision.get("why") or "")):
            return
        if ask := str(decision.get("ask") or "").strip():
            self._ask(outing, ask)
            return
        if say := str(decision.get("say") or "").strip():
            self.deliver(say)
        why = str(decision.get("why") or "").strip()
        go = _number(decision.get("go"))
        takes = [items[n - 1][0] for n in dict.fromkeys(_number(x) for x in decision.get("take") or [])
                 if 1 <= n <= len(items)]
        if not 1 <= go <= len(goals) or len(outing.visited) >= max_places:
            then = ("finish", None, why or "вылазка окончена")
            outing.plans.append(f"закончить вылазку — {why or 'хватит'}")
        else:
            goal = goals[go - 1][0]
            then = ("pending" if pause else "go", goal, why)
            outing.plans.append(f"{goal.name} — {why or 'интересно'}" + (f" (взять: {', '.join(i.name for i in takes)})"
                                                                         if takes else ""))
        if takes:
            outing.takes, outing.then = takes, then
            self._take_next(outing)
        else:
            self._then(outing, then)

    def _talk(self, outing: Outing, talk: str, why: str, near: bool = False) -> bool:
        """Заговорить с человеком рядом («Имя: фраза»), а после разговора — решить заново. False — не с кем или нельзя
        (решение идёт дальше как обычно). near — только что подошёл к нему (игра подтвердила)."""
        name, _, opener = talk.partition(":")
        name, opener = name.strip().strip("«»\"'"), opener.strip().strip("«»\"'")
        # Модель пишет имя, как в списке: «Мила Валентия (имперец)» — с видом в скобках (24.10: «нет рядом»).
        bare = name.split("(")[0].strip().casefold()
        npc = next((n for n in self._crowd() if bare in (n.name.casefold(), n.title.casefold())), None) or next(
            (n for n in self._crowd() if bare and n.name.casefold().startswith(bare)), None)
        # С одним человеком — один разговор за вылазку, всего — не больше TALKS_MAX: иначе модель заводит разговор
        # снова и снова (разговор -> решение -> разговор…).
        blocked = ("разговоры выключены" if not self.talk else f"«{name}» нет рядом" if not npc or not npc.form
                   else "с ним ты уже говорил" if npc.form in outing.talked
                   else f"за вылазку уже {TALKS_MAX} разговоров" if len(outing.talked) >= TALKS_MAX
                   else f"ты поговорил уже с {outing.streak} подряд — сначала место или побудь и послушай"
                   if outing.streak >= TALKS_IN_ROW
                   else self.talk.unavailable(npc, near=True))
        problem = blocked or self.talk.unavailable(npc, near)
        # Подойти — только когда мешает одно расстояние: «уже говорил», «два подряд» — не повод идти к нему (24.10).
        if problem and not blocked and opener and (
                not outing.talk_after or outing.talk_after[0] != npc.form):
            # Отошёл, пока Гослинг слушал (24.10: Олава ушла за 10 с — «далеко»): подойти заново и сразу заговорить.
            outing.talk_after = (npc.form, opener, why)
            trace.event(f"Исследователь: {npc.title} далеко — подхожу, чтобы заговорить")
            # Уже идёт к нему — второй поход не нужен: закончившись, он остановил бы первый.
            if not (outing.goal and outing.goal.actor == npc.form and outing.goal_id):
                self._go(outing, Goal(f"к человеку: {npc.title}", actor=npc.form), "подойти и заговорить")
            return True
        if problem or not opener:
            trace.event(f"Исследователь: заговорить с «{name}» нельзя — {problem or 'нечего сказать'}")
            return False
        outing.talked.add(npc.form)
        outing.streak += 1
        outing.plans.append(f"заговорил с {npc.title}")
        self.game.task = f"разговариваешь с {npc.title}"
        trace.event(f"Исследователь: заговариваю с {npc.title} — «{opener}»")
        lines = self.talk.converse(npc, opener, why)
        if not lines:
            return False
        self._next(outing, f"ты поговорил с {npc.title}: {' / '.join(lines)}. Его слова — его мнение и знание, не "
                           "проверенный факт")
        return True

    def _release(self, outing: Outing) -> None:
        """Держался рядом с человеком (подошёл, говорил) — отойти от него и ждать на месте."""
        if outing.holding:
            outing.holding = False
            self.orders.command("release")

    def _ask(self, outing: Outing, question: str) -> None:
        """Спросить Владоса и ждать: никуда не идёт (поход, если шёл, — стоит на месте: цель та же, решит по ответу)."""
        self._release(outing)  # ждёт ответа на месте, а не ходит за человеком
        outing.asked, outing.asked_at = question, time.time()
        outing.plans.append(f"спросил Владоса: «{question}»")
        self.game.task = f"спросил Владоса: «{question}» — ждёшь ответа"
        log.info("Исследователь: спрашиваю Владоса — %s", question)
        trace.event(f"Исследователь: спрашиваю — «{question}»")
        self.deliver(question)
        self._publish(outing)

    def _then(self, outing: Outing, then: tuple) -> None:
        what, goal, why = then
        if what == "finish":
            self._finish(outing, why)
        elif what == "pending":
            self._release(outing)  # ждёт Владоса на месте
            outing.pending, outing.pending_why, outing.near_since = goal, why, None
            self.game.task = (f"на месте «{self._here()}»: ждёшь, пока Владос подойдёт и осмотрится, потом ведёшь его "
                              f"к цели «{goal.name}»")
            log.info("Исследователь: жду Владоса, потом -> %s (%s)", goal.name, why)
            trace.event(f"Исследователь: жду Владоса на месте, потом — «{goal.name}» — {why}")
            self._publish(outing)
        else:
            self._go(outing, goal, why)

    def _go(self, outing: Outing, goal: Goal, why: str) -> None:
        if outing.goal is goal and outing.goal_id:
            # Решил продолжать ту же цель (драка в пути, «веди» снова) — поход уже идёт: второй GoTo к той же цели,
            # закончившись, остановил бы квест похода и первому. Застрял — скрипт сам скажет «дороги нет».
            trace.event(f"Исследователь: продолжаю — «{goal.name}»")
            return
        outing.pending, outing.near_since, outing.goal, outing.asked, outing.inside_wait = None, None, goal, "", False
        outing.holding = False  # новый поход сам снимает «рядом с человеком» (GoTo)
        lead = outing.mode == "lead"
        self.game.task = f"исследуешь: {'ведёшь Владоса' if lead else 'идёшь'} к цели «{goal.name}» — {why or 'интересно'}"
        self.game.task_done = False
        log.info("Исследователь: -> %s (%s)", goal.name, why)
        trace.event(f"Исследователь: иду — «{goal.name}» — {why}")
        outing.goal_id = (self.orders.command("linger") if goal.linger
                          else self.orders.go_ref(goal.actor, goal.name, lead, arrive=PERSON_NEAR) if goal.actor
                          else self.orders.go_ref(goal.door, goal.name, lead, arrive=INSIDE_NEAR if goal.inside
                                                  else DOOR_NEAR) if goal.door
                          else self.orders.go_spot(goal.spot, goal.name, lead) if goal.spot
                          else self.orders.go_marker(goal.marker, lead))
        self._publish(outing)

    # ------------------------------------------------------------ вещи

    def _take_next(self, outing: Outing) -> None:
        if not outing.takes:
            then, outing.then = outing.then, None
            if then:
                self._then(outing, then)
            return
        self._release(outing)  # к вещи идёт сам — пакет следования тянул бы назад к человеку
        item = outing.takes[0]
        trace.event(f"Исследователь: беру «{item.name}»")
        outing.take_id = self.orders.take(item)
        self._publish(outing)

    def _taken(self, command_id: str, data: dict, order: Order) -> None:
        with self._lock:
            outing = self.outing
        if not outing or command_id != outing.take_id or not outing.takes:
            return
        item = outing.takes.pop(0)
        changes = []
        if data.get("ok"):
            what = f"забрал из «{item.name}» всё, что там было" if item.kind == "container" else f"взял «{item.name}»"
            outing.taken.append(item.name if item.kind != "container" else f"всё из «{item.name}»")
            changes.append(Change("own_loot", f"ты {what} — несёшь Владосу", subject=f"взял: {item.name}",
                                  where=self._here()))
            if left := [str(x) for x in data.get("left") or []]:  # осталось в сундуке — квестовое
                outing.left += [f"{x} (в «{item.name}»)" for x in left]
                changes.append(Change("own_loot", f"в «{item.name}» осталось по заданию: {', '.join(left)} — ты не "
                                                  "трогал, скажи Владосу взять самому", subject=f"оставил: {item.name}",
                                      important=True, where=self._here()))
        else:
            changes.append(Change("own_loot", f"взять «{item.name}» не вышло: "
                                              f"{REASONS.get(str(data.get('reason')), 'что-то помешало')}",
                                  subject=f"не взял: {item.name}", where=self._here()))
        self.record(changes)
        self._take_next(outing)

    def _bag(self) -> dict[int, int]:
        g = self.game.status.gosling
        return {int(r["form"]): int(r.get("count", 1)) for r in (g.bag or [])} if g else {}

    def _finds(self, outing: Outing) -> list[tuple[int, int, str]]:
        """Что прибавилось в сумке за вылазку — находки (не своё снаряжение): (предмет, сколько, название)."""
        g = self.game.status.gosling
        return [(int(r["form"]), int(r.get("count", 1)) - outing.bag_before.get(int(r["form"]), 0), str(r["name"]))
                for r in (g.bag or [] if g else []) if int(r.get("count", 1)) > outing.bag_before.get(int(r["form"]), 0)]

    def _hand_over(self, outing: Outing) -> None:
        """Отдать Владосу находки вылазки — он рядом."""
        finds = self._finds(outing)
        for form, count, name in finds:
            self.orders.hand_over(form, count, name)
        if finds:
            log.info("Исследователь: отдаю Владосу %s", ", ".join(f"{n} ×{c}" for _f, c, n in finds))
            outing.gave += [n if c == 1 else f"{n} ×{c}" for _f, c, n in finds]
            self._publish(outing)

    # ------------------------------------------------------------ конец вылазки и встреча

    def _finish(self, outing: Outing, why: str) -> None:
        with self._lock:
            if self.outing is not outing:
                return
            self.outing = None
        found = f"; нанёс на карту: {', '.join(outing.found)}" if outing.found else ""
        log.info("Исследователь: вылазка закончена — %s%s", why, found)
        trace.event(f"Исследователь: вылазка закончена — {why}{found}")
        self.record([Change("own_note", f"ты закончил вылазку ({why}); побывал: {', '.join(outing.visited) or 'нигде'}"
                                        + found, subject="вылазка окончена", important=True, where=self._here())])
        self.game.task = "вылазка окончена, идёшь к Владосу" if outing.mode == "explore" else None
        outing.holding = False
        self.orders.command("follow")  # к Владосу: игра сама приведёт спутника (и снимет «рядом с человеком»)
        outing.ended = why
        self._publish(outing)
        self._remember(outing)
        if outing.mode == "lead" or self.game.hears():  # Владос рядом — находки сразу
            self._hand_over(outing)

    def _remember(self, outing: Outing) -> None:
        """Одиночная вылазка — рассказать при встрече (и отдать находки)."""
        if outing.mode == "explore" and not outing.told:
            self.last = outing

    def _welcome(self) -> None:
        """Снова вместе после одиночной вылазки — сам рассказывает, как прошла, и отдаёт находки."""
        with self._lock:
            outing, self.last = self.last, None
            busy = self.outing is not None
        if not outing or outing.told or busy:
            return
        outing.told = True
        log_lines = [f"ходил: {', '.join(outing.visited) or 'никуда не дошёл'}",
                     f"нанёс на карту: {', '.join(outing.found)}" if outing.found else "",
                     f"нашёл и несёшь Владосу: {', '.join(n for _f, _c, n in self._finds(outing))}"
                     if self._finds(outing) else "",
                     f"оставил по заданию (пусть возьмёт сам): {', '.join(outing.left)}" if outing.left else "",
                     f"узнал: {'; '.join(outing.learned)}" if outing.learned else "",
                     f"звал или спрашивал Владоса — он не ответил: {'; '.join(outing.unanswered)}"
                     if outing.unanswered else "",
                     "что было: " + "; ".join(self.game.events_since(outing.started))]
        story = self.complete(STORY, "Журнал вылазки:\n" + "\n".join(f"- {x}" for x in log_lines if x),
                              title="Исследователь: рассказ при встрече")
        if isinstance(story, dict) and (say := str(story.get("say") or "").strip()):
            self.deliver(say)
        self._hand_over(outing)

    def _tick(self) -> None:
        """Раз в секунду: предел времени вылазки; «Веди», ждём на месте — Владос подошёл (ближе LEAD_NEAR) и побыл
        рядом explorer.lead_pause — идём дальше (не подходит — ждём, «веди» — сразу)."""
        with self._lock:
            outing = self.outing
        if not outing:
            return
        now = time.time()
        max_minutes = self._limits()[1]
        if now - outing.started > max_minutes * 60 and not outing.takes:
            self._finish(outing, f"вылазка идёт уже {max_minutes:.0f} мин — пора к Владосу")
            return
        if outing.arrival and now - outing.arrival[1] >= outing.arrival[2]:
            self._look(outing)
            return
        if outing.inside_wait and self.game.hears() and not outing.takes:
            # Звал внутрь — Владос пришёл: помещение ожило (люди, разговоры, вещи) — осмотреться вместе и решать.
            outing.inside_wait, outing.asked = False, ""
            outing.arrival = (f"Владос зашёл к тебе — {self._here()}: теперь здесь всё живое, осмотритесь", now,
                              self.cfg.look_around)
            trace.event("Исследователь: Владос пришёл внутрь — осматриваемся")
            self._publish(outing)
            return
        wait = ASK_WAIT_NEAR if self.game.hears() else ASK_WAIT_FAR
        if outing.asked and now - outing.asked_at > wait and not outing.takes:
            outing.unanswered.append(outing.asked)  # «звал Владоса в Йоррваскр — не пришёл»: расскажет потом
            words = (f"; что он говорил: {' / '.join(f'«{w}»' for w in outing.words[-2:])}" if outing.words else "")
            self._next(outing, f"ты спросил Владоса: «{outing.asked}», а ответа на это не было{words} — решай сам",
                       current=outing.goal or outing.pending)
            return
        if not outing.pending or outing.takes:
            return
        g = self.game.status.gosling
        if not (g and g.loaded and 0 <= g.distance <= LEAD_NEAR):
            outing.near_since = None
            return
        outing.near_since = outing.near_since or now
        if now - outing.near_since >= self.cfg.lead_pause:
            self._go(outing, outing.pending, outing.pending_why)

    # ------------------------------------------------------------ блок «Вылазка» для разговора

    def _publish(self, outing: Outing) -> None:
        """Блок «Вылазка» в «Сейчас в игре» и в письмах (GameState.outing): что решил исследователь — режим, что сейчас,
        его решения, где был, что нашёл. Без него разговор и письма видели одну строку дела и могли сказать не то, что
        решил исследователь. Кончилась — «Последняя вылазка» с итогами (explorer.remember_minutes): когда вы снова
        вместе, «что ты там видел?» — по фактам, даже когда «Недавно» уже забыло."""
        lines = []
        if not outing.ended:
            # Куда идти — решение вылазки, о нём она и говорит; разговор и реплики сами по себе его не объявляют (24.10:
            # «Начну с Милы» — и следом «Начинаю с Милы»; «Мила сбежала, пойду к Олаве» — дважды).
            lines.append("куда идёшь и что делаешь дальше, решаешь не в разговоре: сам не объявляй и не обещай — скажешь,"
                         " когда решишь; спросит Владос — отвечай по «сейчас» и «твой план»")
        if outing.ended:
            lines.append(f"чем кончилась: {outing.ended}")
        elif outing.asked:
            lines.append(f"сейчас: спросил Владоса «{outing.asked}» — ждёшь ответа; согласишься с ним — то же "
                         f"«{'lead' if outing.mode == 'lead' else 'explore'}», а куда — в твоих словах")
        elif outing.takes:
            lines.append(f"сейчас: берёшь вещи — {', '.join(i.name for i in outing.takes)}")
        elif outing.pending:
            lines.append(f"сейчас: на месте, ждёшь Владоса, потом — «{outing.pending.name}»")
        elif outing.goal:
            lines.append(f"сейчас: {'ведёшь Владоса' if outing.mode == 'lead' else 'идёшь'} к «{outing.goal.name}»")
        if outing.request:
            lines.append(f"Владос просил: «{outing.request}»")
        if outing.plan and not outing.ended:
            lines.append(f"твой план: {outing.plan}")
        if outing.plans and not outing.ended:
            lines.append(f"твои решения: {'; '.join(outing.plans[-3:])}")
        if outing.visited:
            lines.append(f"был: {', '.join(outing.visited)}")
        if outing.found:
            lines.append(f"нанёс на карту Владоса: {', '.join(outing.found)}")
        if outing.gave:
            lines.append(f"нашёл и отдал Владосу: {', '.join(outing.gave)}")
        elif outing.taken:
            lines.append(f"нашёл, несёшь Владосу: {', '.join(outing.taken)}")
        if outing.left:
            lines.append(f"оставил по заданию (пусть возьмёт сам): {', '.join(outing.left)}")
        if outing.unanswered:
            lines.append(f"спрашивал или звал Владоса — ответа не было: {'; '.join(outing.unanswered)}")
        if outing.learned:
            lines.append(f"узнал: {'; '.join(outing.learned[-LEARNED_SHOWN:])}"
                         + (" (не всё — остальное в памяти о местах)" if len(outing.learned) > LEARNED_SHOWN else ""))
        who = ("с Владосом — ты вёл" if outing.mode == "lead" and outing.ended
               else "с Владосом — ты его ведёшь" if outing.mode == "lead"
               else "сам — Владос ходит рядом с тобой и слышит" if self.game.hears() and not outing.ended else "один")
        self.game.set_outing(who, "; ".join(lines), ended=bool(outing.ended), keep=self.cfg.remember_minutes * 60)

    # ------------------------------------------------------------ данные игры

    def _goals(self, outing: Outing, current: Goal | None = None) -> list[tuple[Goal, str]]:
        """Цели: продолжить текущую (драка в пути); в поселении — его центр и здания, куда заходят без приглашения;
        внутрь — в «Веди», у входа в подземелье (особые точки локации); ближайшие маркеры карты вокруг Гослинга —
        открытые Владосом с названием, неоткрытые — только вид."""
        g, s = self.game.status.gosling, self.game.status
        goals: list[tuple[Goal, str]] = []
        if current:
            goals.append((current, f"продолжить: {current.name}"))
        here = g.location if g else s.location
        # Побыть здесь и послушать — где есть люди и мир вокруг живой (Владос рядом): таверна, площадь, зал замка. Только
        # что побыл — снова с другого места: первым в списке, он выбирал бы его раз за разом.
        if self.game.hears() and not outing.stayed and (crowd := [n for n in self._crowd() if not n.hostile and not n.dead]):
            goals.append((Goal("побыть здесь и послушать", linger=True),
                          f"побыть здесь ~{self.cfg.linger / 60:.1f} мин: постоять, присесть, послушать, о чём говорят "
                          f"вокруг (людей рядом: {len(crowd)})"))
        # Люди вокруг — подойти и послушать (24.10: в Вайтране слышал Хеймскра, Назима, стражу — и шёл к следующей
        # двери): за что зацепиться. Живые, не враги; что человек говорил — к цели. В списке — после мест: первыми он
        # их и брал, и вылазка становилась обходом людей (24.10: пять разговоров подряд, ни одного здания). Наговорился
        # подряд (TALKS_IN_ROW) — людей нет, пока не сходит к месту или не побудет и послушает.
        people: list[tuple[Goal, str]] = []
        for n in (self._crowd() if outing.streak < TALKS_IN_ROW else []):
            if not n.form or n.hostile or n.dead:
                continue
            goal = Goal(f"к человеку: {n.title}", actor=n.form)
            if goal.key in outing.failed or (current and current.key == goal.key):
                continue
            said = [f"«{h.text}»" for h in list(self.game.heard) if h.speaker == n.name and not h.to_player][-PERSON_HEARD:]
            near = n.gosling_distance if n.gosling_distance is not None else n.distance  # от него, не от Владоса
            notes = ([f"~{round(near)} м от тебя"] if near is not None else []) + (
                [f"говорил: {' / '.join(said)}"] if said else []) + (
                ["уже слушал — можно вернуться"] if goal.key in outing.done else []) + (
                ["ты с ним уже говорил"] if n.form in outing.talked else [])
            people.append((goal, goal.name + (f" ({'; '.join(notes)})" if notes else "")))
        # Где стоим — не только куда дошёл своим походом: Владос привёл сам или поход отменили у самого входа (24.10:
        # у Факельной шахты «пошли в шахту, исследуем» — а «внутрь» не было).
        place = (self.places.find(here) if self.places and here else None) or outing.place
        town = self.places.settlement_of(place) if self.places and place else None
        skip = outing.done | outing.failed | ({current.key} if current else set())
        outing.closed = []
        if town:
            # По поселению: «походи, посмотри Морфал» — а в списке только места вокруг, и он ушёл к форту (24.10).
            # Здания — из файлов игры (дочерние локации), все: и чужие дома — помечены, заходить ли, решает он (не
            # уверен — спросит Владоса). Заперто (двери вокруг него — плагин) — не пойти: взламывать он не умеет.
            doors = {d.to: d for d in (g.doors or [])} if g else {}
            spots = [(Goal(f"центр — {town.name}", spot=town.center(), place=town.name), "")]
            # Главное — первым (замок, храм, таверна…), внутри — ближние; чужие дома — последними.
            for b in sorted(self.places.buildings(town), key=lambda b: (
                    b.importance, (doors[b.name].distance or 0) if b.name in doors else float("inf"))):
                if b.key == place.key:
                    continue
                door = doors.get(b.name)
                if door and door.locked:
                    outing.closed.append(f"{b.name} — заперто")
                    continue
                kinds = ', '.join(b.kinds()) or 'здание'
                notes = ([f"~{round(door.distance)} м"] if door and door.distance is not None else []) + (
                    ["чужой дом — хозяева не звали"] if b.private else []) + ([] if door else ["заперто ли — не видно"])
                # Один — сначала улица: у входа мир живой (Владос в городе), а внутри без него пусто — помещение
                # просчитывается, только когда он там (24.10: в Йоррваскре один — ни людей, ни разговоров).
                if outing.mode == "explore" and door and door.form:
                    spots.append((Goal(f"{b.name} ({kinds}) — у входа", place=b.name, door=door.form), "; ".join(notes)))
                inside = "; ".join(notes + (["встанешь сразу за дверью; без Владоса внутри пусто — позовёшь его, "
                                             "войдёт — оживёт"] if outing.mode == "explore" else []))
                # Внутрь — к той же двери изнутри (плагин): сразу за входом, где Владос, войдя, его и увидит (середина
                # здания — где попало внутри, 24.10); двери не видно — к середине.
                spots.append((Goal(f"{b.name} ({kinds}) — зайти внутрь", door=door.inside, place=b.name, inside=True)
                              if door and door.inside else
                              Goal(f"{b.name} ({kinds}) — зайти внутрь", spot=b.center(), place=b.name), inside))
            # Все здания, без предела: видеть надо всё (24.10: с пределом 12 в Вайтране выпадали дома). Где уже был —
            # тоже, с пометкой: «зайди к Соратникам» после Йоррваскра — «его нет в списке» (24.10).
            for goal, notes in spots:
                if (goal.spot or goal.door) and goal.key not in outing.failed and not (current and current.key == goal.key):
                    notes = "; ".join(x for x in [notes, "уже был — можно вернуться" if goal.key in outing.done else ""] if x)
                    goals.append((goal, f"{goal.name} (в этом поселении{'; ' + notes if notes else ''})"))
        elif outing.mode == "lead" and place and self.game.hears():
            kinds = ", ".join(place.kinds())
            for spot in place.spots:
                goal = Goal(f"внутрь «{place.name}»: {spot.kind}", spot=spot, place=place.name)
                if goal.key not in outing.done | outing.failed and not (current and current.key == goal.key):
                    goals.append((goal, goal.name + (f" ({kinds})" if kinds else "")))
        goals += people
        pos = (g.pos if g and g.pos else None) or s.pos
        if not pos:
            return goals
        radius = self.cfg.lead_radius if outing.mode == "lead" else self.cfg.radius
        nearby = sorted(((distance_m(pos[0], pos[1], m), m) for m in self.game.markers
                         if m.form and f"marker {m.form}" not in skip and m.name != here), key=lambda x: x[0])
        nearby = [(d, m) for d, m in nearby if d >= MIN_DISTANCE]
        close = [(d, m) for d, m in nearby if d <= radius][:self.cfg.candidates]
        # Дальше — по ближайшему месту каждого вида, которого рядом нет: «пойдём в город» — а рядом одни хижины и руины
        # (21.10: восемь ближайших — в 350 м, города среди них нет, и исследователь молча повёл к руинам).
        kinds = {m.type for _d, m in close}
        far = []
        for d, m in nearby:
            if d > self.cfg.radius or len(far) >= FAR_KINDS:
                break
            if m.type not in kinds:
                kinds.add(m.type)
                far.append((d, m))
        for d, m in close:
            goals.append((Goal(_name(m), marker=m), f"{_name(m)} — {format_distance(d)} на {direction(pos[0], pos[1], m)}"))
        for d, m in far:
            goals.append((Goal(_name(m), marker=m), f"{_name(m)} — {format_distance(d)} на {direction(pos[0], pos[1], m)} "
                                                    "(дальше; ближайшее место этого вида)"))
        return goals

    def _items(self, outing: Outing) -> tuple[list[tuple[ItemNear, str]], list[str]]:
        """Что рядом можно взять (ничьё, не по заданию, не заперто, не пустое) — и что брать нельзя, с причиной.
        Только загруженный и Владос рядом: вдали вещей игра не просчитывает."""
        g = self.game.status.gosling
        if not g or not g.items or not self.game.hears():
            return [], []
        can, cant = [], []
        for it in g.items:
            if it.quest:
                cant.append(f"{it.name} — по заданию: скажи Владосу взять самому")
            elif it.owned:
                cant.append(f"{it.name} — чужое: взять — кража")
            elif it.kind == "container" and it.locked:
                cant.append(f"{it.name} — заперт")
            elif it.kind == "container" and not it.count:
                continue
            else:
                what = (f"сундук, вещей: {it.count}" if it.kind == "container"
                        else f"{it.value} зол." if it.value else "")
                can.append((it, f"{it.name}" + (f" — {what}" if what else "")
                            + (f", {round(it.distance)} м" if it.distance is not None else "")))
        return can, cant

    def _limits(self) -> tuple[int, float]:
        """Пределы вылазки по месту: в поселении — целей и минут больше (там очень много всего: 24.10, в Вайтране
        коротко пробежался и захотел назад); иначе — explorer.max_places / max_minutes."""
        g = self.game.status.gosling
        here = (g.location if g else "") or self.game.status.location
        place = self.places.find(here) if self.places and here else None
        if place and self.places.settlement_of(place):
            return self.cfg.town_places, self.cfg.town_minutes
        return self.cfg.max_places, self.cfg.max_minutes

    def _subject(self) -> str:
        """О чём факт: поселение, в котором он (о таверне Морфала — «Морфал»), иначе место, где он."""
        g = self.game.status.gosling
        here = (g.location if g else "") or self.game.status.location or "дикие земли"
        place = self.places.find(here) if self.places else None
        town = self.places.settlement_of(place) if place else None
        return town.name if town else here

    def _balance(self, outing: Outing) -> str:
        """Чем была вылазка: разговоры, места, «побыть здесь» — чтобы видел перекос (24.10: одни разговоры)."""
        places = sum(1 for v in outing.visited if not v.startswith("к человеку: "))
        text = f"разговоров {len(outing.talked)}, мест (у входа, внутри, на карте) {places}, побыл и послушал {outing.lingered}"
        if outing.streak >= TALKS_IN_ROW:
            text += f" — поговорил с {outing.streak} подряд: теперь место или побудь и послушай, люди — потом"
        return text

    def _player(self) -> str:
        """Где Владос — по игре: рядом (слышит, видит то же) или далеко."""
        g = self.game.status.gosling
        if self.game.hears():
            near = f" (~{round(g.distance)} м)" if g and g.distance >= 0 else ""
            return (f"рядом{near}, ходит с тобой — слышит тебя и видит то же: с ним говоришь вслух (say, ask), "
                    "с людьми — talk")
        return "далеко — с ним только письмом (say, ask)"

    def _pos(self) -> tuple[float, float] | None:
        g = self.game.status.gosling
        return tuple(g.pos[:2]) if g and g.pos else None

    def _here(self) -> str:
        g = self.game.status.gosling
        if not g:
            return ""
        place = g.location or ("дикие земли" if g.location == "" else "")
        return f"{place}, {g.cell}" if g.cell and g.cell != g.location and place else (place or g.cell or "")


def _name(m: Marker) -> str:
    return f"{m.name} ({m.kind})" if m.visible else f"неизвестное место ({m.kind}, нет на карте Владоса)"


def _number(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
