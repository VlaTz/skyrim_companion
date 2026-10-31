"""Ответы компаньона: любая LLM с OpenAI-совместимым API (LM Studio, Ollama, OpenRouter ...).

Модель получает промпт личности из characters/<персонаж>/prompt.md и отвечает
JSON-ом {"emotion": ..., "text": ...}: текст озвучивается, эмоция выбирает
референсы голоса.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from companion.config import LLMConfig
from companion import trace
from companion.emotions import DEFAULT_EMOTION, EMOTIONS
from companion.orders import ACTIONS
from companion.tools import Tool

log = logging.getLogger(__name__)

RESPONSE_FORMAT = """
ФОРМАТ ОТВЕТА
Отвечай строго одним JSON-объектом, без пояснений и markdown:
{{"emotion": "<эмоция>", "text": "<что ты говоришь вслух>", "remember": "<необязательно>"}}
"emotion" — с какой интонацией ты это произносишь, одно из: {emotions}.
Выбирай по ситуации и смыслу реплики; по умолчанию — neutral.
"remember" — узнал что-то новое и важное (о месте, человеке, задании, о Владосе): что запомнить, одной фразой.
Только действительно важное, не каждый раз; нечего запоминать — не пиши поле. Говоришь, что запомнишь, —
заполни его.
"solved" — Владос в этой реплике объяснил, почему так, одну из «Загадок» из памяти — хоть коротко («я Довакин, боги
меня возвращают»), хоть ты не уверен, что он прав: "<её название, как в «Загадки»>: <как он объяснил, одной фразой>".
Тогда ты больше об этом не спрашиваешь, а думаешь так, как он сказал. Твои собственные догадки — не разгадка.
Объяснения не было — не пиши поле.
"idea" — в этой реплике ты высказал (хоть своими словами, хоть мимоходом) одну из «Твоих мыслей» из обстановки:
её метка, например "m2". Не высказывал — не пиши поле.
"felt" — в ответ на реплику Владоса: как она тебя задела, если задела: {{"чувство": "радость|обида|злость|страх|
утешение", "сила": 1..3}}. Тёплые слова, похвала, доброе о тебе — радость; грубость всерьёз — обида или злость;
тревога, опасность рядом — страх; поддержка после беды — утешение. Подколка в шутку и обычный разговор (куда идём,
что делать) — не пиши поле.
"do" — сделать в игре то, что ты в этой реплике согласился сделать по просьбе Владоса: "wait" — остаться ждать
здесь (на время, он вернётся); "follow" — ТЫ идёшь ЗА НИМ, ведёт он: «пошли за мной», «иди сюда», «ко мне»,
«теперь я веду», «хватит, пошли» (ты не его спутник — значит, пойти с ним; ведёшь его или исследуешь — это конец
вылазки: идёшь к нему); "dismiss" — он
отпускает тебя совсем: ты больше не его спутник и уходишь к себе, туда, где он тебя нашёл; "go_to" — сходить к
месту с карты Владоса, тогда ещё "place" — его название точно как на карте (ближайшие — в «Сейчас в игре»,
любые другие — справка find_places) и "back": true, если потом вернуться к Владосу, false — ждать его там.
Идёшь пешком, по дорогам: до дальнего места путь займёт время. "explore" — Владос отпускает тебя исследовать одного:
куда идти, решишь сам по дороге, вернёшься после нескольких мест; "lead" — ОН идёт ЗА ТОБОЙ, ведёшь ты: только
когда он просит вести его — «веди», «показывай дорогу», «покажи, что тут интересного», «веди дальше» (уже ведёшь —
"lead" значит «пошли дальше»). «За мной», «я веду», «иди сюда» — это не "lead", а "follow". Начиная "explore" или
"lead", конкретное место не называй, если его не назвал Владос: куда идти, ты решишь по дороге, — скажи, что поведёшь.
В вылазке ты спросил Владоса, куда идти (блок «Вылазка»: «спросил … — ждёшь ответа»), и он ответил — согласен с ним:
тот же "lead" или "explore", а куда — скажи в text. Звал его зайти вместе, и он идёт с тобой («пошли», «давай») —
"lead"; «иди сам», «дальше без меня» — "explore".
"talk" — заговорить с человеком рядом, когда Владос просит: «поговори с ним», «спроси у неё, …», «узнай у стражника …»;
тогда ещё "who" — его имя, как в «Рядом», и "opener" — что ты скажешь ему первым (о том, о чём просил Владос). Ты
подойдёшь к нему (пойдёшь следом, если он идёт), поговоришь — он ответит сам — и вернёшься рассказать Владосу. Что он
ответит, ты пока не знаешь: не разыгрывай разговор в text и не говори за него, даже если помнишь, что он говорил
раньше, — скажи только, что идёшь поговорить. Просят спросить кого-то из «Рядом» — это всегда "talk". Его нет в
«Рядом» — скажи, что не видишь его тут, и поле не пиши.
text — Владосу, а не ему: к человеку ты обратишься в "opener", когда подойдёшь. «Спроси у стражника, где тут зелья» —
{{"text": "Сейчас спрошу.", "do": "talk", "who": "Стражник Вайтрана", "opener": "Подскажи, где тут купить зелья?"}}.
Сказал «жду», «иду за тобой», «иду», «пошли», «ну, бывай», «сбегаю туда», «пошёл осмотрюсь», «веду» — заполни поле:
без него ты этого не сделаешь.
Отказался, пошутил, переспрашиваешь или уточняешь, что он хочет, или просто говорите о планах — не пиши поле:
вопрос — ещё не согласие, сначала дождись ответа. Не понял, о чём он (одно слово, название без просьбы, «сходим
туда» — а куда, неясно), — переспроси, поле не пиши. Уже делаешь то, о чём речь (ждёшь, а он не зовёт идти; идёшь
за ним), — поле не повторяй: «жди» на непонятную реплику оставляет тебя на месте, а Владос уходит.
"""

# Действия голосом — поле "do" ответа (companion/orders.py), выполняет игра. Чего пока нет — честно говорим модели.
CAPABILITIES = """
ТВОИ ВОЗМОЖНОСТИ
По просьбе Владоса (поле "do") ты можешь:
- остаться ждать на месте; идти за ним (ведёт он); расстаться с ним и уйти к себе, туда, где он тебя нашёл;
- сходить к месту с его карты и вернуться или ждать его там — идёшь пешком, по дорогам, хоть далеко;
- уйти исследовать одному: сам выбираешь, куда идти, по дороге пишешь ему, после нескольких мест возвращаешься;
- вести его исследовать вместе: выбираешь ты и идёшь впереди, он — за тобой; отстанет — ждёшь его;
- заговорить с человеком рядом, когда он попросит: подойдёшь, поговоришь (человек ответит сам, своими словами) и
  вернёшься рассказать — только когда Владос рядом: вдали люди тебе не ответят.
Место, которого нет на его карте, дойдя, ты наносишь на его карту — ты картограф. Сражаешься ты сам, как обычный
спутник; в бою тебя не убить — падаешь на колено и встаёшь.
В вылазке, когда ведёшь Владоса, ты сам решаешь зайти внутрь подземелья и подобрать находки — ничьё; из сундука
берёшь всё, кроме вещей по заданию (их оставляешь Владосу), чужое не трогаешь; находки отдаёшь ему при встрече.
В поселении, исследуя (одному или ведя Владоса), можешь походить по нему: центр, замок, таверна, лавки, храм и
чужие дома (туда — если уверен, что Владос не против, иначе спросишь); запертое не открываешь. Не понял, чего он
хочет, — переспросишь.
По отдельной просьбе голосом («возьми тот меч», «зайди внутрь») ты так пока не умеешь: не обещай и не говори, что
уже делаешь, — скажи, что сделаешь это сам, когда поведёшь его исследовать.
Когда ты далеко от Владоса, ты его не видишь и не слышишь; что он делал без тебя, ты не знаешь. Издалека вы
переписываетесь: его слова приходят тебе письмом, твой ответ он читает на экране; позовёт — придёшь.
"""

SELF_TALK = """
КАК УСТРОЕНЫ СООБЩЕНИЯ
В начале последнего сообщения — блок «Сейчас в игре»: самая свежая обстановка. Если она расходится
с тем, что ты говорил раньше, верь ей — вы могли уже уйти, войти куда-то, закончить бой.
«Игрок: …» — слова игрока. На них отвечай всегда, пустой text нельзя.
Сообщения в квадратных скобках — не слова игрока, а то, что сейчас происходит. На них ты можешь
сказать что-то от себя (коротко, к месту, не повторяя уже сказанное) или промолчать — тогда
верни пустой text. Молчать — нормально: лучше промолчать, чем болтать без повода.
Пометки в событиях — из твоей памяти: «впервые в жизни» — ты такого ещё не видел, это первая встреча;
«уже встречали … последний — там-то» — ты это помнишь и можешь вспомнить тот раз.
"""
TOOLS_NOTE = """
СПРАВКИ
У тебя есть инструменты-справки об игре. Вызывай их, только если для ответа нужно то, чего нет в
«Сейчас в игре», — каждая справка задерживает ответ. Получив справку, отвечай как обычно, JSON-ом.
Справка и есть «посмотреть», и она мгновенная: если ответ без неё был бы «сейчас гляну», «посмотрю» — вызови
её в этом же ответе. Посмотреть «потом» ты не сможешь: следующего хода может и не быть.
Не пересказывай справку целиком — скажи главное, как сказал бы спутнику вслух.
"""
GAME_CONTEXT_NOTE ="Описана в начале последнего сообщения, в блоке «Сейчас в игре»."


BETWEEN_MAX = 8  # событий между репликами в истории — последних

# Обращение к игроку. «Не в каждой реплике» в промпте персонажа модель не держит (131 из 140 реплик в памяти —
# с обращением, 79 с него начинаются): своим прошлым репликам в истории она подражает сильнее. Поэтому — по факту:
# было обращение в последних ADDRESS_LOOKBACK репликах — в этой без него (пометка только в этот запрос).
ADDRESS = re.compile(r"\b(Владос\w*|братан\w*|бро)\b", re.I)
ADDRESS_LOOKBACK = 2
ADDRESS_NOTE = ("(Ты уже обращался к нему в прошлых репликах — в этой говори без обращения: без «Владос», "
                "«братан», «бро».)")

# Длина ответа — по ситуации, как в живом разговоре. Правило в промпте модель не держит: на «Тук-тук» — 18 слов,
# на реплики до 5 слов — медиана 23 (журнал 16.10, 53 ответа). Поэтому — пометка к ходу (только в этот запрос):
# Владос сказал коротко — ответь в тон; идёт бой — пара слов. Длинная реплика — без пометки: модель решает сама.
SHORT_WORDS = 6
# Число, а не «коротко»: «часто одной фразой» модель не держала (медиана 17 -> 17,5 слова), «до 12 слов» — 8,5 (19.10).
LENGTH_SHORT = ("(Владос сказал коротко — ответь в тон: одной фразой, до 12 слов. Подробнее — только если он просит "
                "рассказать или объяснить.)")
LENGTH_COMBAT = "(Идёт бой — ответь в пару слов, по делу.)"
# Переписка (companion/letters.py): Гослинг далеко — слова Владоса пришли письмом, ответ прочтут на экране.
LETTER_PREFIX = "Письмо от Владоса: "
LETTER_NOTE = ("(Вы с Владосом далеко друг от друга и переписываетесь: это его письмо, твой ответ — тоже письмо, его "
               "прочтут на экране, голоса нет: 1–3 коротких предложения. Что вокруг него, ты не видишь — только то, что "
               "он пишет; что вокруг тебя — в «Сейчас в игре». Зовёт к себе — \"do\": \"follow\", ты придёшь сам.)")

# Планировщик справок: отдельный короткий запрос без истории разговора — нужны ли справки для ответа игроку.
# С историей модель держится своих прошлых ответов без справок («сейчас гляну» — и не смотрит: 0 из 4 на ходе
# из трассы), без истории — зовёт справку (4 из 4). Идёт параллельно с основным запросом, ответ не замедляет.
ROUTER_SYSTEM = """Ты помогаешь спутнику игрока в Скайриме ответить на последнюю реплику игрока. Реши, какие
справки (инструменты) для этого нужны, и вызови их — можно несколько сразу. Справка нужна, если ответ зависит
от того, чего нет в «Сейчас в игре», или игрок просит что-то посмотреть, проверить, найти. Просьба посмотреть
(«смотри», «посмотри», «что видишь», «как тебе вид», «кто это») — всегда look: описание вида в «Сейчас в игре» взято
раньше, а игрок просит взглянуть сейчас и мог повернуться. Разговор о мире — людях, фракциях, народах, войне,
истории, богах, книгах, — в котором ответ назовёт факт о нём (вопрос, сомнение, «надо бы узнать», «то есть X — это Y,
да?»), — world_info, даже если спутник уже говорил об этом: его прошлые слова о мире — не источник. Просьба
поговорить с кем-то или спросить у него («спроси у стражника…», «узнай у Хеймскра, о чём он кричит») — это не справка,
а дело спутника: подойти и спросить самому; справки для неё не нужны. Справки не нужны —
ответь одним словом: нет."""
ROUTER_HISTORY = 4  # последних сообщений (2 обмена) — чтобы понять «посмотри» и «а там что?»
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm")


def between_note(events: list[str]) -> str:
    """Что случилось между репликами — строкой в квадратных скобках (не слова игрока)."""
    return "[С твоей прошлой реплики в игре: " + "; ".join(events[-BETWEEN_MAX:]) + "]"


@dataclass
class Reply:
    text: str
    emotion: str
    interest: int | None = None  # насколько зацепило (0–10) — только если об этом спрашивали в запросе
    remember: str | None = None  # что запомнить (модель решила, что узнала важное)
    joke: str | None = None      # id заготовки шутки, которую обыграла (companion/jokes.py)
    quip: bool = False           # прозвучала шуткой ниже порога интереса
    idea: str | None = None      # метка мысли из «Твоих мыслей», которую высказала (companion/memory/thinker.py)
    felt: tuple[str, int] | None = None  # как задела реплика Владоса: (чувство, сила 1..3) — настроению
    do: str | None = None        # что сделать в игре (wait | follow | dismiss | go_to) — companion/orders.py
    place: str | None = None     # go_to: куда — название места с карты
    back: bool = False           # go_to: потом вернуться к Владосу (иначе ждать там)
    who: str | None = None       # talk: с кем заговорить — имя, как в «Рядом»
    opener: str | None = None    # talk: что сказать ему первым
    solved: str | None = None    # Владос объяснил загадку: «загадка: как» (companion/mysteries.py)


FELT = ("радость", "обида", "злость", "страх", "утешение")


def parse_felt(value) -> tuple[str, int] | None:
    """{"чувство": "...", "сила": 1..3} -> (чувство, сила); непонятное — None."""
    if not isinstance(value, dict):
        return None
    feeling = str(value.get("чувство") or "").strip().lower()
    try:
        strength = max(1, min(3, int(value.get("сила") or 1)))
    except (TypeError, ValueError):
        strength = 1
    return (feeling, strength) if feeling in FELT else None


def parse_reply(content: str) -> Reply:
    """Достаёт {"emotion", "text"} из ответа модели; не-JSON считается репликой с neutral."""
    match = re.search(r"\{.*\}", content, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            text = str(data.get("text", "")).strip()
            emotion = str(data.get("emotion", "")).strip().lower()
            if text or "text" in data:  # пустой text — модель решила промолчать
                try:
                    interest = int(data["interest"]) if "interest" in data else None
                except (TypeError, ValueError):
                    interest = None
                remember = str(data.get("remember") or "").strip() or None
                joke = str(data.get("joke") or "").strip() or None
                idea = str(data.get("idea") or "").strip() or None
                do = str(data.get("do") or "").strip().lower()
                return Reply(text, emotion if emotion in EMOTIONS else DEFAULT_EMOTION, interest, remember, joke,
                             idea=idea, felt=parse_felt(data.get("felt")), do=do if do in ACTIONS else None,
                             place=str(data.get("place") or "").strip() or None, back=data.get("back") is True,
                             who=str(data.get("who") or "").strip() or None,
                             opener=str(data.get("opener") or "").strip() or None,
                             solved=str(data.get("solved") or "").strip() or None)
        except json.JSONDecodeError:
            pass
    log.warning("LLM ответила не чистым JSON: %r", content)
    # JSON оборван (например, по max_tokens) — берём текст, который успел прийти, а не сырой JSON.
    if text_match := re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)', content):
        try:
            text = json.loads(f'"{text_match.group(1)}"').strip()
        except json.JSONDecodeError:
            text = text_match.group(1).strip()
        # Не озвучивать оборванное слово: обрезаем до последнего целого предложения, если оно есть.
        if (end := max(text.rfind(c) for c in ".!?…")) > 0:
            text = text[:end + 1]
        emotion_match = re.search(r'"emotion"\s*:\s*"(\w+)"', content)
        emotion = emotion_match.group(1).lower() if emotion_match else ""
        if text:
            return Reply(text, emotion if emotion in EMOTIONS else DEFAULT_EMOTION)
    return Reply(content.strip().strip('"'), DEFAULT_EMOTION)


def length_note(player_text: str, in_combat: bool) -> str:
    """Пометка о длине ответа на реплику игрока: в бою — пара слов, на короткую реплику — коротко."""
    if in_combat:
        return LENGTH_COMBAT
    if len(player_text.split()) <= SHORT_WORDS:
        return LENGTH_SHORT
    return ""


class CompanionLLM:
    def __init__(self, cfg: LLMConfig, prompt_file: Path):
        from openai import OpenAI

        if not prompt_file.is_file():
            raise FileNotFoundError(f"Нет промпта личности {prompt_file}")
        self.cfg = cfg
        self.prompt = (prompt_file.read_text(encoding="utf-8").strip() + "\n" + CAPABILITIES + SELF_TALK
                       + RESPONSE_FORMAT.format(emotions=", ".join(EMOTIONS)))
        # Локальным серверам ключ не нужен, но клиент требует непустую строку.
        api_key = cfg.api_key or (os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else "")
        local = any(h in cfg.base_url for h in ("localhost", "127.0.0.1", "[::1]"))
        if not api_key and not local:
            raise RuntimeError(
                f"Нет ключа для {cfg.base_url}: укажи llm.api_key в config.local.yaml "
                f"или переменную окружения {cfg.api_key_env}")
        self.client = OpenAI(base_url=cfg.base_url, api_key=api_key or "not-needed",
                             timeout=cfg.timeout, max_retries=1)
        self.history: deque[dict] = deque(maxlen=cfg.history_turns * 2)
        self._lock = threading.Lock()  # отвечают и на реплики игрока, и на события — по очереди
        self.tools: dict[str, Tool] = {}
        # Что случилось в игре после момента t (сжато) — в историю перед следующей репликой, чтобы модель видела
        # порядок: «я сказал “помоги ему” -> Арвел напал -> Владос его убил -> Владос спрашивает».
        self.events_since: Callable[[float], list[str]] = lambda t: []
        self._last_turn = 0.0  # когда последний раз дописали историю
        self.last_reply_at = 0.0  # когда Гослинг последний раз что-то сказал (мозгу: не болтает ли подряд)
        self.last_reply_text = ""
        self.background_model = cfg.background_model or cfg.model
        log.info("LLM: %s @ %s (фоновые запросы: %s)", cfg.model, cfg.base_url, self.background_model)

    def system_prompt(self, memory: str = "", with_tools: bool = False) -> str:
        # Обстановка — не здесь, а в последнем сообщении: так модель не тянется за старыми ответами.
        prompt = (self.prompt
                  .replace("{memory}", memory or "Пока ничего — вы только познакомились.")
                  .replace("{game_context}", GAME_CONTEXT_NOTE))
        return prompt + TOOLS_NOTE if with_tools else prompt

    def set_tools(self, tools: list[Tool]) -> None:
        self.tools = {t.name: t for t in tools}
        log.info("Инструменты агента: %s", ", ".join(self.tools) or "нет")

    def reply(self, player_text: str, game_context: str = "", memory: str = "", in_combat: bool = False,
              letter: bool = False) -> Reply:
        """Ответ на реплику игрока. Молчать в ответ нельзя — пустой ответ переспрашиваем один раз.
        letter — Гослинг далеко, это письмо Владоса, ответ — тоже письмом (companion/letters.py)."""
        note = LETTER_NOTE if letter else length_note(player_text, in_combat)
        asked = f"{LETTER_PREFIX}{player_text}" if letter else f"Игрок: {player_text}"
        reply = self._ask(asked, game_context, memory, use_tools=True, note=note)
        if not reply.text:
            log.warning("LLM промолчала в ответ игроку — переспрашиваем")
            reply = self._ask(asked, game_context, memory, use_tools=True, note=note)
        return reply

    def initiate(self, situation: str, game_context: str = "", memory: str = "",
                 min_interest: int | None = None, quip_chance: float = 0.0, reasoning: str = "",
                 max_tokens: int | None = None) -> Reply:
        """Реплика по своей инициативе на событие или тишину; пустой text — решил промолчать.
        min_interest — ответ ниже этой оценки interest (её просит situation) считается молчанием; но с шансом
        quip_chance он всё же звучит — ниже порога модель пишет короткую шутку (так просит situation).
        reasoning — рассуждение модели перед ответом (low | medium | high) для этого запроса: обычно оно выключено
        ради скорости (llm.extra_body), а размышлению в тишине спешить некуда; max_tokens — лимит с ним.
        Без инструментов: тут важнее быстро."""
        return self._ask(f"[{situation}]", game_context, memory, use_tools=False, min_interest=min_interest,
                         quip_chance=quip_chance, reasoning=reasoning, max_tokens=max_tokens)

    def _ask(self, user_content: str, game_context: str, memory: str, use_tools: bool,
             min_interest: int | None = None, quip_chance: float = 0.0, reasoning: str = "",
             max_tokens: int | None = None, note: str = "") -> Reply:
        """user_content уходит в историю как есть; обстановка «Сейчас в игре» и обмен
        с инструментами — только в этот запрос."""
        now = f"Сейчас в игре:\n{game_context or 'Нет данных из игры.'}\n\n{user_content}"
        tools = list(self.tools.values()) if use_tools and self.cfg.tools else []
        first_line = user_content.splitlines()[0][:150] if user_content else ""
        from_player = user_content.startswith(("Игрок:", LETTER_PREFIX))
        block = trace.Block(first_line if from_player else f"Сам: {first_line}")
        if not from_player:
            block.section("Повод", user_content)
        block.section("Сейчас в игре", game_context or "нет данных")
        block.section("Память", memory or "пусто")
        try:
            return self._ask_traced(user_content, now, memory, tools, min_interest, quip_chance, block,
                                    reasoning, max_tokens, note)
        except Exception as e:
            block.line(f"ОШИБКА: {e!r}")
            raise
        finally:
            block.write()

    def _ask_traced(self, user_content: str, now: str, memory: str, tools: list[Tool],
                    min_interest: int | None, quip_chance: float, block: trace.Block,
                    reasoning: str = "", max_tokens: int | None = None, note: str = "") -> Reply:
        with self._lock:
            if note:  # длина ответа по ситуации — только в этот запрос, не в историю
                now += f"\n{note}"
                block.line(f"Пометка: {note.strip('()')}")
            if self._addressed_lately():  # история меняется только под блокировкой — смотрим здесь
                now += f"\n{ADDRESS_NOTE}"
                block.line("Пометка: без обращения (оно было в прошлых репликах)")
            messages = [{"role": "system", "content": self.system_prompt(memory, with_tools=bool(tools))},
                        *self.history,
                        {"role": "user", "content": now}]

            t = time.perf_counter()
            calls = 0
            # Цикл агента: модель может запросить справки (до max_tool_rounds раз), потом отвечает.
            for round_ in range(self.cfg.max_tool_rounds + 1):
                if round_ == 0 and tools and self.cfg.tool_router:
                    msg = self._first_round(messages, now, tools, block)
                else:
                    msg = self._complete(messages, tools, final=round_ == self.cfg.max_tool_rounds,
                                         reasoning=reasoning, max_tokens=max_tokens)
                if not msg.tool_calls:
                    break
                messages.append({"role": "assistant", "content": msg.content or "",
                                 "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
                for tc in msg.tool_calls:
                    calls += 1
                    started = time.perf_counter()
                    result = self._run_tool(tc)
                    block.line(f"→ {tc.function.name}({tc.function.arguments}) "
                               f"{(time.perf_counter() - started) * 1000:.0f} мс")
                    block.line(f"← {result}")
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            reply = parse_reply(msg.content or "")
            block.line(f"Ответ модели {time.perf_counter() - t:.2f} с"
                       + (f" (с рассуждением: {reasoning})" if reasoning else "") + f": [{reply.emotion}]"
                       + (f" интерес {reply.interest}" if reply.interest is not None else "")
                       + f" «{reply.text}»" + (f"\n  запомнить: {reply.remember}" if reply.remember else "")
                       + (f"\n  обыграл заготовку: {reply.joke}" if reply.joke else "")
                       + (f"\n  высказал мысль: {reply.idea}" if reply.idea else "")
                       + (f"\n  задело: {reply.felt[0]} {reply.felt[1]}" if reply.felt else "")
                       + (f"\n  сделать: {reply.do}" + (f" → {reply.place}" if reply.place else "")
                          + (f" → {reply.who}: «{reply.opener}»" if reply.who else "")
                          + (", потом назад" if reply.back else "") if reply.do else ""))
            log.info("LLM %.2f с%s: [%s]%s %r", time.perf_counter() - t,
                     f" (инструментов: {calls})" if calls else "", reply.emotion,
                     f" интерес {reply.interest}" if reply.interest is not None else "", reply.text)
            if min_interest is not None and (reply.interest or 0) < min_interest and reply.text:
                if random.random() < quip_chance:  # не важно, но иногда — короткой шуткой
                    log.info("Интерес %s ниже порога %d — но сказал шуткой (шанс %.0f%%)",
                             reply.interest, min_interest, quip_chance * 100)
                    block.line(f"→ ниже порога {min_interest}, но сказал шуткой (шанс {quip_chance:.0%})")
                    reply.quip = True
                else:
                    log.info("Интерес %s ниже порога %d — промолчал", reply.interest, min_interest)
                    block.line(f"→ промолчал: интерес {reply.interest} ниже порога {min_interest}")
                    reply.text = ""

            if reply.text:  # молчание на событие в историю не пишем
                # События с прошлой реплики — в историю вместе с этой (в сам запрос они уже вошли «Недавно»).
                between = [e for e in self.events_since(self._last_turn) if e not in user_content]
                if self._last_turn and between:
                    user_content = f"{between_note(between)}\n{user_content}"
                self._last_turn = self.last_reply_at = time.time()
                self.last_reply_text = reply.text
                self.history.append({"role": "user", "content": user_content})
                # "do" — тоже: иначе через пару ходов модель не помнит, что согласилась ждать.
                said = {"emotion": reply.emotion, "text": reply.text} | ({"do": reply.do} if reply.do else {})
                if reply.do == "go_to":
                    said |= {"place": reply.place, "back": reply.back}
                elif reply.do == "talk":
                    said |= {"who": reply.who, "opener": reply.opener}
                self.history.append({"role": "assistant", "content": json.dumps(said, ensure_ascii=False)})
            return reply

    def _first_round(self, messages: list[dict], now: str, tools: list[Tool], block: trace.Block):
        """Первый шаг ответа игроку: основной запрос и планировщик справок — параллельно. Планировщик назвал
        справки, а модель сама их не позвала — берём его план, как будто это её вызовы (её ответ без справок
        отбрасываем); иначе — её ответ как есть."""
        first = _pool.submit(self._complete, list(messages), tools, False)
        planned = self._route(now, tools, block)
        if planned:
            try:
                own = first.result() if first.done() else None
            except Exception:  # noqa: BLE001 — основной запрос упал, а план есть: ответим по плану
                own = None
            if not (own and own.tool_calls):
                return SimpleNamespace(content="", tool_calls=planned)
        return first.result()

    def _route(self, now: str, tools: list[Tool], block: trace.Block) -> list:
        """Планировщик справок: какие инструменты вызвать (их tool_calls) или [] — не нужны."""
        lines = []
        for m in list(self.history)[-ROUTER_HISTORY:]:
            if m["role"] == "assistant":
                try:
                    lines.append("Ты: " + str(json.loads(m["content"]).get("text", "")))
                except json.JSONDecodeError:
                    lines.append("Ты: " + m["content"])
            else:
                lines.append(m["content"].splitlines()[-1])  # без «[С твоей прошлой реплики …]»
        user = ("Последние реплики:\n" + "\n".join(lines) + "\n\n" if lines else "") + now
        t = time.perf_counter()
        try:
            resp = self.client.chat.completions.create(
                model=self.cfg.model, temperature=0, max_tokens=300,
                messages=[{"role": "system", "content": ROUTER_SYSTEM}, {"role": "user", "content": user}],
                tools=[tool.schema() for tool in tools], extra_body=self.cfg.extra_body or None)
            planned = resp.choices[0].message.tool_calls or []
        except Exception as e:  # noqa: BLE001 — без плана модель сама решит, звать ли справки
            log.warning("Планировщик справок не ответил: %s", e)
            planned = []
        names = ", ".join(f"{c.function.name}({c.function.arguments})" for c in planned)
        block.line(f"План справок {time.perf_counter() - t:.2f} с: {names or 'не нужны'}")
        return planned

    def _complete(self, messages: list[dict], tools: list[Tool], final: bool, reasoning: str = "",
                  max_tokens: int | None = None):
        kwargs: dict = {}
        extra_body = dict(self.cfg.extra_body or {})
        if reasoning:
            extra_body["reasoning"] = {"effort": reasoning}
        if self.cfg.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if tools:
            kwargs["tools"] = [t.schema() for t in tools]
            if final:  # справок хватит — пора отвечать (сами tools оставляем: в истории есть их вызовы)
                kwargs["tool_choice"] = "none"
        resp = self.client.chat.completions.create(
            model=self.cfg.model, messages=messages,
            temperature=self.cfg.temperature, max_tokens=max_tokens or self.cfg.max_tokens,
            extra_body=extra_body or None, **kwargs,
        )
        if resp.choices[0].finish_reason == "length":
            log.warning("Ответ LLM обрезан по max_tokens (%d)", max_tokens or self.cfg.max_tokens)
        return resp.choices[0].message

    def _run_tool(self, call) -> str:
        name = call.function.name
        t = time.perf_counter()
        try:
            args = json.loads(call.function.arguments or "{}")
            tool = self.tools.get(name)
            result = tool.handler(**args) if tool else f"Нет такого инструмента: {name}"
        except Exception as e:  # noqa: BLE001 — ошибка инструмента не должна ронять ответ
            log.exception("Инструмент %s упал", name)
            args, result = call.function.arguments, f"Ошибка инструмента: {e}"
        log.info("Инструмент %s(%s) %.0f мс -> %r", name, args, (time.perf_counter() - t) * 1000, result[:200])
        return result[:3000]

    def reset(self) -> None:
        self.history.clear()

    def _addressed_lately(self) -> bool:
        """В последних ADDRESS_LOOKBACK репликах Гослинга было обращение к игроку."""
        replies = [m["content"] for m in self.history if m["role"] == "assistant"][-ADDRESS_LOOKBACK:]
        return any(ADDRESS.search(r) for r in replies)

    def complete_json(self, system: str, user: str, max_tokens: int | None = None,
                      title: str = "Фоновый запрос", model: str | None = None, reasoning: str = "") -> dict | None:
        """Отдельный запрос без истории и инструментов (дневник и т.п.): ответ — JSON-объект или None.
        Не берёт блокировку разговора — идёт в фоне, пока Гослинг болтает. Модель — llm.background_model
        (или model: шутки пишет jokes.model). reasoning — глубина рассуждения для этого запроса (low | medium |
        high): душа — решение «кто я», ему нужно подумать."""
        model = model or self.background_model
        block = trace.Block(f"{title} [{model}" + (f", рассуждение {reasoning}" if reasoning else "") + "]")
        block.section("Запрос", user)
        try:
            return self._complete_json(system, user, max_tokens or self.cfg.background_max_tokens, block, model,
                                       reasoning)
        finally:
            block.write()

    def _complete_json(self, system: str, user: str, max_tokens: int, block: trace.Block,
                       model: str, reasoning: str = "") -> dict | None:
        extra_body = dict((self.cfg.extra_body if self.cfg.background_extra_body is None
                           else self.cfg.background_extra_body) or {})
        if reasoning:
            extra_body["reasoning"] = {"effort": reasoning}
        try:
            resp = self.client.chat.completions.create(
                model=model, temperature=0.3, max_tokens=max_tokens,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                extra_body=extra_body or None)
        except Exception as e:  # noqa: BLE001 — сеть, лимиты: фоновая задача просто попробует позже
            log.warning("Фоновый запрос к LLM не удался: %s", e)
            block.line(f"ОШИБКА: {e!r}")
            return None
        content = resp.choices[0].message.content or ""
        block.section("Ответ", content)
        match = re.search(r"\{.*\}", content, re.S)
        try:
            return json.loads(match.group(0)) if match else None
        except json.JSONDecodeError:
            # Бывает: JSON, потом «исправляюсь» и второй JSON — берём последний целый объект.
            last, decoder, pos = None, json.JSONDecoder(), 0
            while (start := content.find("{", pos)) >= 0:
                try:
                    obj, pos = decoder.raw_decode(content, start)  # дальше — после объекта, не внутрь него
                except json.JSONDecodeError:
                    pos = start + 1
                    continue
                if isinstance(obj, dict):
                    last = obj
            if last is None:
                log.warning("Фоновый запрос: не JSON: %r", content[:300])
            return last

    def drop_last(self, reply: Reply) -> None:
        """Ответ так и не прозвучал (игрок перебил) — убрать его вместе с вопросом из истории."""
        with self._lock:
            if len(self.history) < 2 or not reply.text:
                return
            try:
                last = json.loads(self.history[-1]["content"]).get("text")
            except (json.JSONDecodeError, AttributeError):
                return
            if last == reply.text:
                self.history.pop()
                self.history.pop()

    def note_said(self, text: str, emotion: str = DEFAULT_EMOTION) -> None:
        """Гослинг сказал (или написал) готовое, без запроса разговора — решил исследователь (companion/explorer.py):
        в историю, как реплику по своей инициативе, — иначе на «а куда мы идём?» модель не знает, что сама сказала."""
        with self._lock:
            self.history.append({"role": "user", "content": "[Ты заговорил сам]"})
            self.history.append({"role": "assistant",
                                 "content": json.dumps({"emotion": emotion, "text": text}, ensure_ascii=False)})
            self._last_turn = self.last_reply_at = time.time()
            self.last_reply_text = text

    def restore(self, dialogue: list[tuple[str, str, str | None, bool]]) -> None:
        """История разговора из журнала памяти (после перезапуска сервиса или смены персонажа):
        [(kind, text, emotion, initiative)], kind — player | gosling | event (что было между репликами)."""
        with self._lock:
            self.history.clear()
            asked: str | None = None
            events: list[list] = []  # [текст, сколько раз подряд] с прошлой реплики Гослинга
            for kind, text, emotion, initiative in dialogue:
                if kind == "event":
                    if events and events[-1][0] == text:
                        events[-1][1] += 1
                    else:
                        events.append([text, 1])
                    continue
                if kind == "player":
                    asked = text  # без ответа (ошибка, перебили) — вопрос заменит следующий
                    continue
                user = f"Игрок: {asked}" if asked is not None and not initiative else "[Ты заговорил сам]"
                if events and self.history:
                    user = between_note([t if n == 1 else f"{t} ×{n}" for t, n in events]) + "\n" + user
                self.history.append({"role": "user", "content": user})
                self.history.append({"role": "assistant", "content": json.dumps(
                    {"emotion": emotion or DEFAULT_EMOTION, "text": text}, ensure_ascii=False)})
                asked, events = None, []
            # Последней реплики больше нет в памяти (ушла в архив с загрузкой сейва постарше) — мозгу её не
            # показываем: «Ты сам говорил 10 с назад: «Мирмулнир мёртв»», а бой ещё впереди.
            if not any(kind == "gosling" and text == self.last_reply_text for kind, text, _e, _i in dialogue):
                self.last_reply_at, self.last_reply_text = 0.0, ""
            self._last_turn = time.time()
        log.info("История разговора восстановлена из памяти: %d реплик", len(self.history) // 2)
