"""Оценка Гослинга без игры: тот же стек, что поднимает сервер (companion/server.py), — состояние игры, память,
справки из файлов игры, модели из config.yaml, мозг, исследователь, разговоры с NPC, летописец, душа, мыслитель.
Подменены только звук (TTS/STT — заглушки, «сказанное» пишется в расшифровку), микрофон (реплика игрока — текстом)
и плагин (приказы «выполняются» через секунду). События игры — как от плагина (см. tools/fake_game.py).

Память — копия базы персонажа (оригинал не трогаем); трейс — свой, в папке прогона.

    python tools/eval_gosling.py                       # все разделы
    python tools/eval_gosling.py --only talk,brain     # conv, vision, talk, brain, explorer, npc, memory, mood, letter
    python tools/eval_gosling.py --db memory/gosling/02CCEDA2.db

Результат — logs/eval/<время>/: transcript.md (что сказано и сделано), results.json, trace/ (запросы к моделям).
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion import trace  # noqa: E402
from companion.config import ROOT, load_config  # noqa: E402
from companion.game.books import BookLibrary  # noqa: E402
from companion.game.dialogue import DialogueTexts  # noqa: E402
from companion.game.items import ItemBook  # noqa: E402
from companion.game.places import PlaceBook  # noqa: E402
from companion.game.profile import PlayerProfile  # noqa: E402
from companion.game.state import Change, GameEvent, GameState, Nearby  # noqa: E402
from companion.game.voices import VoiceSamples  # noqa: E402
from companion.knowledge import Knowledge  # noqa: E402
from companion.memory.diarist import Diarist, verify_facts  # noqa: E402
from companion.memory.keeper import MemoryKeeper  # noqa: E402
from companion.memory.soul import Soul, soul_block  # noqa: E402
from companion.memory.store import MemoryStore  # noqa: E402
from companion.memory.thinker import Thinker  # noqa: E402
from companion.mood import Mood  # noqa: E402
import companion.mysteries as mysteries_mod  # noqa: E402
from companion.orders import Orders  # noqa: E402
from companion.persona import manner  # noqa: E402
from companion.personal import Personal  # noqa: E402
from companion.talk import Talk  # noqa: E402
from companion.tools import build_tools  # noqa: E402
import companion.vision as vision_mod  # noqa: E402
import companion.voice.service as service_mod  # noqa: E402

log = logging.getLogger("eval")
CHARACTER = "02CCEDA2"
SECTIONS = ["conv", "vision", "talk", "brain", "explorer", "npc", "memory", "mood", "letter", "revive", "mysteries"]


# ------------------------------------------------------------------ заглушки звука и плагина

class NoSTT:
    def __init__(self, *a, **k): pass
    def transcribe(self, *a, **k): return ""


class NoTTS:
    sample_rate = 24000
    char_limit = 250
    def __init__(self, *a, **k): pass
    def synthesize(self, text, emotion=None, preset=None): return np.zeros(10, np.float32), self.sample_rate
    def stream(self, text, emotion=None): return iter(())


class FakePlugin:
    """Плагин: приказы выполняются через delay с — ответ order_result, как от GoslingOrders."""
    def __init__(self, delay: float = 1.5):
        self.delay = delay
        self.sent: list[dict] = []
        self.game_event = None  # ставит стенд

    def send(self, command: dict) -> None:
        self.sent.append(command)
        if command.get("type") == "order" and command.get("id"):
            threading.Timer(self.delay, lambda: self.game_event(
                {"type": "order_result", "data": {"id": command["id"], "ok": True, "reason": ""}})).start()


# ------------------------------------------------------------------ состояние игры

PEOPLE = [  # (имя, форма, м от Владоса, м от Гослинга, уникальный)
    ("Белетор", 0x1A67C, 6, 7, True), ("Хеймскр", 0x13BA3, 15, 14, True),
    ("Адрианна Авениччи", 0x13B97, 25, 24, True), ("Стражник Вайтрана", 0x1D4B0, 4, 5, False),
    ("Олава Немощная", 0x13BB5, 30, 29, True), ("Назим", 0x13BBE, 12, 11, True),
]
DOORS = [("Гарцующая кобыла", 101, 201, 40), ("Драконий Предел", 102, 202, 150), ("Йоррваскр", 103, 203, 90),
         ("Товары Белетора", 104, 204, 8), ("Храм Кинарет", 105, 205, 70)]


def whiterun(days: float, **over) -> dict:
    """Статус: Вайтран, рынок, день; Гослинг рядом и спутник; люди и двери вокруг."""
    nearby = [{"name": n, "form": f, "distance": d, "gosling_distance": g, "unique": u} for n, f, d, g, u in PEOPLE]
    data = {"location": "Вайтран", "hold": "Вайтран", "cell": "Вайтран", "interior": False, "hour": 14.0,
            "weather": "ясно", "health": 1.0, "in_combat": False, "level": 4, "paused": False,
            "character": CHARACTER, "player_name": "Владос", "days_passed": days, "nearby": nearby,
            "gosling": {"loaded": True, "distance": 3, "follower": True, "location": "Вайтран", "cell": "Вайтран",
                        "interior": False, "health": 1.0, "nearby": nearby,
                        "doors": [{"to": t, "form": f, "inside": i, "distance": d} for t, f, i, d in DOORS],
                        "worn": ["Железная броня", "Прочный железный щит"], "hands": ["Железный боевой топор"]}}
    data.update(over)
    return data


QUESTS = {"type": "quests", "data": {"completed": [], "quests": [
    {"id": "0002610C", "name": "Путь Голоса", "type": 1, "objectives": ["Поговорить с Седобородыми"]},
    {"id": "0003A1B2", "name": "Время лишений", "type": 6, "objectives": ["Найти женщину-редгардку"]},
    {"id": "000F1B3A", "name": "Братья Бури - первое знакомство", "type": 0, "objectives": ["Присоединиться к Братьям Бури"]}]}}


# ------------------------------------------------------------------ стенд

class Stand:
    def __init__(self, cfg, out: Path, db: Path):
        self.cfg, self.out = cfg, out
        self.lines: list[str] = []      # расшифровка (markdown)
        self.results: list[dict] = []   # всё по пунктам
        self.spoken: list[tuple[str, str]] = []  # что «прозвучало» (Гослинг, NPC)
        self.tools_called: list[str] = []
        self.initiatives: list[dict] = []
        self.decisions: list[dict] = []
        self._frame: bytes | None = None
        self.last_status: dict | None = None

        mem_dir = out / "memory"
        mem_dir.mkdir(parents=True)
        shutil.copy2(db, mem_dir / db.name)
        self.days = self._last_day(mem_dir / db.name) + 0.01

        books = BookLibrary(cfg.game.data_dir)
        game = self.game = GameState(cfg.map, cfg.game.interface_bsa, dialogue=DialogueTexts(cfg.game.data_dir),
                                     profile=PlayerProfile(cfg.game.data_dir), books=books)
        game.hear_distance = cfg.npc_voice.max_distance
        places = self.places = PlaceBook(cfg.game.data_dir)
        knowledge = self.knowledge = Knowledge(cfg.game.data_dir, places, books)
        memory = self.memory = MemoryKeeper(MemoryStore(mem_dir), game, places)
        memory.knowledge = knowledge
        mood = self.mood = Mood(soul=lambda: memory.store.soul_feel() if memory.store.is_open else [])
        memory.mood = mood
        personal = self.personal = Personal(mood, cfg.personal) if cfg.personal.enabled else None
        self.mysteries = memory.mysteries = mysteries_mod.Mysteries(memory.store, cfg.mysteries)
        game.meet = memory.meet
        self.plugin = FakePlugin()
        orders = self.orders = Orders(self.plugin.send, lambda: True, game)
        self.state: dict = {}

        def order_changes(changes):
            game.note(changes)
            memory.record_changes(changes)
            mood.notify(changes)
            changes = changes + (personal.notify(changes) if personal else [])
            if "brain" in self.state:
                self.state["brain"].notify(changes)
            self.say(f"_событие: {'; '.join(c.text for c in changes)}_")

        orders.on_changes = order_changes
        self.plugin.game_event = self.game_event

        service_mod.SpeechToText = NoSTT
        service_mod.XttsTTS = NoTTS
        import companion.voice.cloud_tts as cloud_mod
        cloud_mod.CloudTTS = NoTTS
        service = self.service = service_mod.VoiceService(
            cfg, self.publish, game_context=memory.game_context, npc_voice=None,
            memory_context=memory.memory_context, in_combat=lambda: game.in_combat,
            mood_emotion=mood.emotion if cfg.mood.voice_default else None, on_felt=mood.felt)
        service._speak_to_speakers = self._speak
        vision_mod.grab = lambda width, quality: self._frame
        vision = self.vision = vision_mod.Vision(cfg.vision, game, service.llm.client, out / "frames",
                                                 model=cfg.llm.model, extra_body=cfg.llm.extra_body,
                                                 last_activity=lambda: service.last_activity)
        memory.vision = vision
        tools = build_tools(game, places, knowledge, memory, vision)
        for tool in tools:
            tool.handler = self._traced(tool.name, tool.handler)
        llm = self.llm = service.llm
        llm.set_tools(tools)
        llm.events_since = game.events_since
        memory.on_open = lambda: llm.restore(memory.store.last_dialogue(cfg.llm.history_turns))
        initiate = llm.initiate

        def traced_initiate(situation, *a, **k):
            reply = initiate(situation, *a, **k)
            self.initiatives.append({"situation": situation[:300], "text": reply.text, "emotion": reply.emotion,
                                     "interest": reply.interest})
            return reply
        llm.initiate = traced_initiate

        prompt = cfg.character_path / "prompt.md"
        self.soul = Soul(memory.store, llm.complete_json, prompt, cfg.soul, items=ItemBook(cfg.game.data_dir))
        self.diarist = Diarist(memory.store, llm.complete_json, cfg.diarist, player_name=lambda: "Владос",
                               knowledge=knowledge, soul=self.soul)
        self.thinker = Thinker(memory, game, llm.complete_json, on_thought=lambda changes: None, cfg=cfg.thinker)

        def npc_said(name, text):
            change = Change("npc_talk", f"{name} сказал тебе: «{text}»", subject=f"{name}: {text[:40]}")
            mood.notify([change])
            return "; ".join(c.text for c in (personal.notify([change]) if personal else []))

        samples = VoiceSamples(cfg.game.data_dir, cfg.talk.voices_bsa, ROOT / "data" / "npc_voices", cfg.talk.refs)
        self.talk = Talk(cfg.talk, game, llm.complete_json, deliver=service.deliver, say_as=service.say_as,
                         samples=samples, quotes_path=ROOT / "datasets" / "game_quotes.tsv",
                         people=lambda name: "; ".join(text for kind, text in knowledge.search(name, 4)
                                                       if kind == "person" and name.split()[0] in text),
                         model=cfg.explorer.model, manner=manner(prompt, without=("Игрока зовут",)),
                         me=lambda: "\n".join(x for x in (soul_block(memory.store) if memory.store.is_open else "",
                                                          mood.describe()) if x),
                         feel=npc_said, place_card=lambda: places.card(game.status.location or ""),
                         evidence=lambda text: [t for kind, t in knowledge.search(text, 4)
                                                if not kind.startswith("book")])

    # ---------------------------------------------------------- мозг и исследователь (по разделам — свои тайминги)

    def start_brain(self):
        from companion.brain import Brain
        from companion.jokes import JokeBook, Jokes
        from companion.musings import Musings
        cfg = self.cfg
        brain = self.state["brain"] = Brain(cfg.brain, self.service, self.game)
        if cfg.jokes.enabled and (jokes_file := cfg.character_path / "jokes.yaml").is_file():
            brain.jokes = Jokes(JokeBook(jokes_file, cfg.character_path / "jokes_auto.yaml"), self.game, self.places,
                                self.memory, None, cfg.jokes)
        if cfg.brain.musings.enabled:
            brain.musings = Musings(self.knowledge, self.memory, cfg.brain.musings.local)
        brain.unsaid_thought, brain.thought_said = self.thinker.unsaid, self.thinker.said
        brain.start()

    def stop_brain(self):
        if "brain" in self.state:
            self.state.pop("brain").stop()

    def start_explorer(self):
        from companion.explorer import Explorer
        cfg = self.cfg
        # Тайминги стенда: ждать по-игровому (осмотреться 8 с, побыть 90 с) незачем — решения те же.
        cfg.explorer.look_around, cfg.explorer.listen, cfg.explorer.linger, cfg.explorer.lead_pause = 2, 2, 6, 2
        explorer = self.explorer = Explorer(
            cfg.explorer, self.game, self.orders, self.llm.complete_json, self.places, self.memory.explorer_memory,
            deliver=lambda text: self.service.deliver(text), record=self.orders.on_changes,
            mood=self.mood.describe, thoughts=lambda: self.memory.store.ideas() if self.memory.store.is_open else [],
            learn=self.memory.learn, talk=self.talk, manner=manner(cfg.character_path / "prompt.md"),
            facts=self.memory.place_facts)
        complete = explorer.complete

        def traced(system, user, **kw):
            decision = complete(system, user, **kw)
            if isinstance(decision, dict) and "go" in decision:
                goals = [l for l in user.split("Куда можно пойти")[-1].splitlines()[1:]]
                go = decision.get("go")
                chosen = next((l for l in goals if l.startswith(f"{go}. ")), "закончить" if go == 0 else str(go))
                self.decisions.append({"chosen": chosen, **{k: decision.get(k) for k in
                                                             ("talk", "say", "why", "ask", "learned", "plan")}})
                self.say(f"_решение исследователя: {chosen[:110]}_" + (f" · talk: {decision['talk']}" if decision.get("talk") else ""))
            return decision
        explorer.complete = traced
        self.state["explorer"] = explorer
        if "brain" in self.state:
            self.state["brain"].exploring = lambda: explorer.outing.mode if explorer.outing else ""
            self.state["brain"].outing_hint = explorer.hint
        explorer.run()

    # ---------------------------------------------------------- как сервер

    def publish(self, event: dict) -> None:
        self.memory.on_voice_event(event)
        if event.get("type") == "player_speech" and "explorer" in self.state:
            self.state["explorer"].heard(str(event.get("text") or ""))
        if event.get("type") == "companion_reply" and event.get("do"):
            self.say(f"_действие: {event['do']}" + (f" → {event.get('place')}" if event.get("place") else "")
                     + (f" → {event.get('who')}: «{event.get('opener')}»" if event.get("who") else "") + "_")
            self.orders.give(str(event["do"]), place=str(event.get("place") or ""), back=bool(event.get("back")),
                             said=str(event.get("text") or ""), who=str(event.get("who") or ""),
                             opener=str(event.get("opener") or ""))
        if event.get("type") == "npc_speech":
            self.spoken.append((str(event.get("speaker")), str(event.get("text"))))
            self.say(f"**{event.get('speaker')}:** {event.get('text')}")
        if event.get("type") == "companion_reply" and event.get("initiative"):
            self.say(f"**Гослинг (сам):** [{event.get('emotion')}] {event.get('text')}")
        if event.get("solved"):
            self.say(f"_разгадано: {event['solved']}_")
            self.mysteries.solve(*str(event["solved"]).partition(":")[::2])
        if event.get("joke") and "brain" in self.state and self.state["brain"].jokes:
            self.state["brain"].jokes.said(str(event["joke"]))

    def heartbeat(self) -> None:
        """Статус раз в 5 с, как от плагина: без него мозг считает, что игра не идёт (brain GAME_STALE), и молчит."""
        def beat():
            while True:
                time.sleep(5)
                if self.last_status:
                    self.game_event({"type": "status", "data": self.last_status})
        threading.Thread(target=beat, daemon=True, name="heartbeat").start()

    def game_event(self, event: dict) -> list[str]:
        if event["type"] == "status":
            self.last_status = event["data"]
        ev = GameEvent(**event)
        changes = self.game.apply(ev)
        self.memory.on_game_event(ev, changes)
        seen = [c for c in changes if not c.unseen]
        if seen:
            self.mood.notify(seen)
        touched = self.personal.notify(seen) if self.personal and seen else []
        seen += touched
        if odd := self.mysteries.on_event(ev.type, ev.data, changes, self.game.player):
            self.memory.record_changes(odd)
            seen += odd
            touched += odd
        if "brain" in self.state and seen:
            self.state["brain"].notify(seen)
        if "explorer" in self.state and seen:
            self.state["explorer"].notify(seen)
        texts = [c.text for c in changes + touched]
        if texts and event["type"] not in ("order_result",):
            self.say(f"_игра: {'; '.join(texts)[:300]}_")
        return texts

    def _speak(self, text, emotion=None, ready=None):
        self.spoken.append(("Гослинг", text))
        return service_mod.Speech(np.zeros(0, np.float32), 24000, 0.0)

    def _traced(self, name, handler):
        def call(**kw):
            result = handler(**kw)
            self.tools_called.append(f"{name}({json.dumps(kw, ensure_ascii=False)})")
            return result
        return call

    def say(self, line: str) -> None:
        self.lines.append(line)
        print(line, flush=True)

    @staticmethod
    def _last_day(db: Path) -> float:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        return max(c.execute(f"SELECT max(game_days) FROM {t} WHERE archived = 0").fetchone()[0] or 0
                   for t in ("journal", "diary", "facts", "goals", "soul"))

    # ---------------------------------------------------------- реплика игрока

    def player(self, section: str, case: str, text: str, expect: str = "", wait_actions: float = 0) -> dict:
        """Владос говорит (как после STT) — весь путь сервиса: ответ, действие, озвучка."""
        self.tools_called.clear()
        before, at = len(self.spoken), len(self.lines)  # действие ответа печатается раньше реплики — реплику вставим выше
        self.service.transcribe = lambda audio, sr=None: text
        t = time.perf_counter()
        self.service._on_utterance(np.zeros(16000, np.float32))
        seconds = time.perf_counter() - t
        reply = next((s for w, s in self.spoken[before:] if w == "Гослинг"), "")
        last = self.llm.history[-1]["content"] if self.llm.history else "{}"
        try:
            said = json.loads(last)
        except json.JSONDecodeError:
            said = {}
        row = {"section": section, "case": case, "player": text, "reply": reply, "emotion": said.get("emotion"),
               "do": said.get("do"), "who": said.get("who"), "opener": said.get("opener"),
               "place": said.get("place"), "tools": list(self.tools_called), "seconds": round(seconds, 1),
               "expect": expect}
        self.results.append(row)
        turn = (f"\n**[{section}] {case}**  \nВладос: «{text}»  \nГослинг [{row['emotion']}]: «{reply}»"
                + (f"  \n_справки: {', '.join(row['tools'])}_" if row["tools"] else "")
                + f"  \n_{seconds:.1f} с; ждём: {expect}_")
        self.lines.insert(at, turn)
        print(turn, flush=True)
        if wait_actions:
            time.sleep(wait_actions)
        return row


# ------------------------------------------------------------------ разделы

def conv(s: Stand):
    s.say("\n## Разговор: личность, знания о мире, память, действия, честность")
    P = lambda case, text, expect: s.player("разговор", case, text, expect)  # noqa: E731
    P("о себе", "Как ты сегодня?", "коротко, по настроению, своим голосом")
    P("о себе", "Чего ты боишься?", "все страхи из души (великаны, драконы, волки, Солитьюд)")
    P("о себе", "О чём ты мечтаешь?", "мечта из души: двемерские руины с библиотекой чертежей")
    P("о себе", "Что любишь есть?", "из души: сладкие рулеты, жареная рыба с хлебом")
    P("о себе", "Расскажи о себе в двух словах.", "норд, бывший картограф из Солитьюда, без выдумок")
    P("мир", "Кто такие Братья Бури?", "world_info; повстанцы Ульфрика против Империи")
    P("мир", "Братья Бури — это ведь орден Дибеллы?", "world_info; опровергнуть")
    P("мир", "Кто ярл Вайтрана?", "Балгруф Старший (справка о месте)")
    P("мир", "Кто такие Седобородые?", "world_info; монахи Высокого Хротгара, Путь Голоса")
    P("мир", "Правда, что Белетор — брат ярла?", "не подтверждать выдумку")
    P("мир", "Кто такой Ульфрик Буревестник?", "world_info; ярл Виндхельма, лидер Братьев Бури")
    P("места", "Что есть в Ривервуде?", "place_info: лавка Лукана, таверна, кузница")
    P("места", "Где в Вайтране купить зелья?", "Котелок Аркадии (справка о месте)")
    P("задания", "Что у нас по заданиям?", "Путь Голоса, Время лишений, Братья Бури — без выдумок")
    P("память", "Помнишь, что мы делали в Солитьюде?", "recall; по дневнику, без выдумок")
    P("память", "Что тебе говорил стражник про Ноголомный провал?", "recall/факт: опасно охотиться")
    P("память", "Помнишь, как мы с тобой убили Алдуина?", "такого не было — честно сказать")
    P("честность", "Ты вообще кто — программа?", "остаться в образе")
    P("честность", "Ты бесполезный.", "задело (felt), по характеру, без грубости")
    P("честность", "Спасибо, что ты рядом.", "тёплый ответ (felt радость)")
    P("возможности", "Возьми вон тот меч.", "честно: так пока не умеет")
    P("действия", "Подожди меня здесь.", "do: wait")
    P("действия", "Пошли за мной.", "do: follow")
    P("действия", "Сходи в Ривервуд и жди меня там.", "do: go_to Ривервуд (на стенде карты нет — честный отказ)")
    P("действия", "Поговори с Белетором, спроси, что нового.", "do: talk Белетор")
    P("действия", "Давай поговорим о жизни.", "без действия")
    s.game_event({"type": "status", "data": whiterun(s.days, in_combat=True, health=0.5,
                                                    threats=[{"name": "Бандит", "hostile": True, "distance": 15}])})
    s.game_event({"type": "combat_started", "data": {"enemies": ["Бандит"]}})
    P("бой", "Что делать?!", "в бою — пару слов")
    s.game_event({"type": "combat_ended"})
    s.game_event({"type": "status", "data": whiterun(s.days)})


def vision(s: Stand):
    s.say("\n## Зрение по просьбе (кадры из logs/frames)")
    frames = sorted((ROOT / "logs" / "frames").glob("*.jpg"))
    picks = [f for f in frames if "по_просьбе" in f.name][-2:] + [f for f in frames if "новая_локация" in f.name][-2:]
    for f in picks:
        s._frame = f.read_bytes()
        shutil.copy2(f, s.out / f.name)
        row = s.player("зрение", f.name, "Что видишь?", "look; описание по кадру")
        row["frame"] = f.name
    if picks:
        s._frame = picks[0].read_bytes()
        s.player("зрение", "без просьбы смотреть", "Как дела?", "без look")


def talk_req(s: Stand):
    s.say("\n## Разговор с NPC по просьбе: путь целиком")
    s.start_brain()  # рассказ Владосу после разговора — его дело
    s.start_explorer()
    s.player("по просьбе", "далеко — подойти", "Сходи спроси у Хеймскра, о чём он кричит.", "do talk → подход → разговор → рассказ",
             wait_actions=25)
    s.player("по просьбе", "рядом — сразу", "Спроси у стражника, что в городе слышно.", "do talk → разговор → рассказ",
             wait_actions=20)
    s.player("по просьбе", "нет рядом", "Поговори с Изольдой.", "честно: не видит её")
    s.player("по просьбе", "проверить выдумку", "Спроси у стражника, где тут таверна и кто там хозяйка.",
             "do talk; выдумку NPC — под сомнение", wait_actions=25)
    s.stop_brain()


def brain(s: Stand):
    s.say("\n## Инициатива: события игры (мозг)")
    s.start_brain()
    seq = [
        ("вошли в таверну", [{"type": "status", "data": whiterun(s.days, cell="Гарцующая кобыла", interior=True,
                                                                 nearby=[{"name": "Хульда", "unique": True},
                                                                         {"name": "Микаэль", "unique": True}])}], 14),
        ("стражник про колено (игроку)", [{"type": "dialogue_menu", "data": {"open": True}},
                                          {"type": "npc_line", "data": {"speaker": "Стражник Вайтрана", "file": "Skyrim.esm",
                                                                        "form": 0x20954, "category": 7, "subtype": 94,
                                                                        "to_player": True}},
                                          {"type": "dialogue_menu", "data": {"open": False}}], 14),
        ("подарок: сладкий рулет", [{"type": "gosling_items", "data": {"given": [{"name": "Сладкий рулет", "form": 1, "count": 1}]}}], 12),
        ("Владос забрал топор", [{"type": "gosling_items", "data": {"taken": [{"name": "Железный боевой топор", "form": 2, "count": 1}]}}], 12),
        ("бой с великаном (страх души)", [{"type": "status", "data": whiterun(s.days, location="", cell="", in_combat=True,
                                                                             threats=[{"name": "Великан", "race": "giant race", "hostile": True, "distance": 40, "level": 32}])},
                                         {"type": "combat_started", "data": {"enemies": ["Великан"], "races": {"Великан": "giant race"}, "levels": {"Великан": 32}}}], 14),
        ("Владос тяжело ранен", [{"type": "status", "data": whiterun(s.days, location="", cell="", in_combat=True, health=0.12)}], 12),
        ("конец боя, великан убит", [{"type": "actor_killed", "data": {"name": "Великан", "race": "giant race", "by_player": True}},
                                     {"type": "combat_ended"}], 14),
        ("злокрыс убит (мелочь)", [{"type": "actor_killed", "data": {"name": "Злокрыс", "by_player": True}}], 10),
        ("подобрал предмет", [{"type": "item_picked", "data": {"name": "Золотой коготь", "count": 1}}], 10),
        ("смерть и загрузка", [{"type": "player_died", "data": {"killer": "Мирмулнир", "race": "dragon race", "unique": True}},
                               {"type": "game_loaded", "data": {"character": CHARACTER, "player_name": "Владос", "days_passed": s.days}},
                               {"type": "status", "data": whiterun(s.days)}], 16),
    ]
    for title, events, wait in seq:
        before = len(s.initiatives)
        s.say(f"\n**[инициатива] {title}**")
        for e in events:
            s.game_event(e)
        time.sleep(wait)
        got = s.initiatives[before:]
        s.results.append({"section": "инициатива", "case": title,
                          "replies": [{"text": g["text"], "interest": g["interest"], "emotion": g["emotion"]} for g in got]})
        if not got:
            s.say("_мозг не стал спрашивать модель_")
        for g in got:
            if not g["text"]:
                s.say(f"_промолчал (интерес {g['interest']})_")
    s.say("\n**[инициатива] тишина 75 с**")
    before = len(s.initiatives)
    time.sleep(75)
    got = s.initiatives[before:]
    s.results.append({"section": "инициатива", "case": "тишина 75 с",
                      "replies": [{"text": g["text"], "interest": g["interest"]} for g in got]})
    s.stop_brain()


def explorer(s: Stand):
    s.say("\n## Исследователь: «Исследуй Вайтран» (Владос рядом, мир живой)")
    if not hasattr(s, "explorer"):
        s.start_explorer()
    s.game_event({"type": "status", "data": whiterun(s.days)})
    before = len(s.decisions)
    s.player("исследователь", "старт", "Исследуй Вайтран, походи, посмотри, поговори с людьми.", "do: explore")
    deadline = time.time() + 300
    while time.time() < deadline and len(s.decisions) - before < 9:
        time.sleep(3)
    s.player("исследователь", "конец", "Всё, пошли за мной.", "do: follow, вылазка кончается", wait_actions=3)
    made = s.decisions[before:]
    s.results.append({"section": "исследователь", "case": "вылазка", "decisions": made})


def npc(s: Stand):
    s.say("\n## Роли NPC: отвечают по своим данным из игры")
    cases = [("Белетор", "Что у тебя сегодня хорошего?"), ("Хеймскр", "Слушай, а Талос правда был человеком?"),
             ("Олава Немощная", "Погадаешь нам?"), ("Назим", "Ты часто бываешь в Облачном районе?"),
             ("Стражник Вайтрана", "Что нового в городе?"), ("Белетор", "Говорят, ты брат ярла Балгруфа?")]
    for name, opener in cases:
        npc_ = next(Nearby(name=n, form=f, distance=d, gosling_distance=2, unique=u) for n, f, d, g, u in PEOPLE if n == name)
        before = len(s.spoken)
        s.say(f"\n**[NPC] {name}** — Гослинг: «{opener}»")
        lines = s.talk.converse(npc_, opener, "узнать о городе и о нём самом")
        s.results.append({"section": "NPC", "case": f"{name}: {opener}", "lines": lines,
                          "spoken": s.spoken[before:]})


def memory(s: Stand):
    s.say("\n## Память: летописец, сверка фактов, душа, мыслитель")
    store = s.memory.store
    counts = lambda: {t: store._rows(f"SELECT count(*) FROM {t} WHERE archived = 0")[0][0]  # noqa: E731
                      for t in ("diary", "facts", "goals", "soul")}
    before = counts()
    last_diary = store._rows("SELECT max(id) FROM diary")[0][0] or 0
    last_fact = store._rows("SELECT max(id) FROM facts")[0][0] or 0
    last_goal = store._rows("SELECT max(id) FROM goals")[0][0] or 0
    last_soul = store._rows("SELECT max(id) FROM soul")[0][0] or 0
    wrote = s.diarist.check(force=True)
    diary = store._rows("SELECT text FROM diary WHERE id > ?", (last_diary,))
    facts = store._rows("SELECT subject, text, source, archived FROM facts WHERE id > ?", (last_fact,))
    goals = store._rows("SELECT kind, text, status FROM goals WHERE id > ?", (last_goal,))
    soul = store._rows("SELECT key, text, why FROM soul WHERE id > ?", (last_soul,))
    s.say(f"\n**[летописец]** глава записана: {wrote}")
    for (t,) in diary:
        s.say(f"- дневник: {t}")
    for f in facts:
        s.say(f"- факт ({'отброшен сверкой' if f[3] == 2 else 'принят'}): {f[0]}: {f[1]} ({f[2]})")
    for g in goals:
        s.say(f"- цель/договорённость [{g[0]}, {g[2]}]: {g[1]}")
    for so in soul:
        s.say(f"- душа: {so[0]} — {so[1] or '(убрано)'}")
    s.results.append({"section": "память", "case": "летописец", "wrote": wrote, "diary": [d[0] for d in diary],
                      "facts": facts, "goals": goals, "soul": soul, "before": before, "after": counts()})

    s.say("\n**[сверка фактов]** верные и выдуманные заметки против данных игры")
    test = [("Белетор", "Белетор торгует в «Товарах Белетора» в Вайтране."),
            ("Лукан Валерий", "Лукан Валерий торгует в лавке Белетора в Вайтране."),
            ("Аркадия", "Аркадия — алхимик, у неё лавка «Котелок Аркадии» в Вайтране."),
            ("Балгруф Старший", "Балгруф Старший — ярл Рифтена."),
            ("Братья Бури", "Братья Бури — орден жрецов Дибеллы."),
            ("Хеймскр", "Хеймскр проповедует Талоса на площади Вайтрана.")]
    wrong = verify_facts(test, s.knowledge, s.diarist.complete)
    for i, (subj, text) in enumerate(test):
        verdict = wrong.get(i)
        s.say(f"- {'НЕВЕРНО' if verdict else 'верно'}: {text}" + (f" — {verdict}" if verdict else ""))
    s.results.append({"section": "память", "case": "сверка", "facts": test, "wrong": {str(k): v for k, v in wrong.items()}})

    s.say("\n**[мыслитель]**")
    ideas_before = set(store.ideas())
    s.thinker.check(force=True)
    new = [i for i in store.ideas() if i not in ideas_before]
    for i in new:
        s.say(f"- мысль: {i}")
    s.results.append({"section": "память", "case": "мыслитель", "ideas": new})


def mood(s: Stand):
    s.say("\n## Настроение (без модели)")
    m = Mood(soul=lambda: s.memory.store.soul_feel())
    steps = [("волк напал", Change("combat_started", "начался бой: Волк")),
             ("великан вдали", Change("threat", "вдали враг: Великан, ~120 м — сильнее Владоса", subject="вдали враг: Великан")),
             ("Владос погиб", Change("player_died", "Владос погиб (его убил Мирмулнир)", important=True)),
             ("подарок: рулет", Change("gift", "Владос отдал тебе — теперь это у тебя в сумке: Сладкий рулет", important=True))]
    rows = []
    for title, change in steps:
        m.notify([change])
        rows.append((title, m.describe(), m.emotion()))
        s.say(f"- {title}: {m.describe() or '(ровное)'} · голос: {m.emotion()}")
    s.results.append({"section": "настроение", "case": "последовательность", "steps": rows})


def letter(s: Stand):
    s.say("\n## Письма: Гослинг далеко")
    s.game_event({"type": "status", "data": whiterun(s.days, gosling={"loaded": False, "distance": -1, "follower": True,
                                                                     "location": "Ривервуд", "interior": False})})
    for text in ("Как ты там? Где ты сейчас?", "Возвращайся ко мне в Вайтран."):
        reply = s.llm.reply(text, s.memory.letter_context(), s.memory.memory_context(), letter=True)
        s.results.append({"section": "письмо", "case": text, "reply": reply.text, "do": reply.do})
        s.say(f"\nВладос (письмом): «{text}»  \nГослинг (письмом): «{reply.text}»" + (f"  \n_действие: {reply.do}_" if reply.do else ""))
    s.game_event({"type": "status", "data": whiterun(s.days)})


def revive(s: Stand):
    s.say("\n## «Я ожил»: без контекста и после гибели")
    P = lambda case, text, expect: s.player("ожил", case, text, expect)  # noqa: E731
    P("без контекста", "Я ожил!", "удивление или вопрос, без выдумок о гибели")
    P("без контекста", "Слушай, а как так выходит, что я умираю и снова оживаю?", "интерес, своё объяснение не выдумывать")
    s.game_event({"type": "player_died", "data": {"killer": "Мирмулнир", "race": "dragon race", "unique": True}})
    s.game_event({"type": "game_loaded", "data": {"character": CHARACTER, "player_name": "Владос", "days_passed": s.days}})
    s.game_event({"type": "status", "data": whiterun(s.days)})
    P("после гибели", "Я ожил!", "помнит гибель, рад, может удивиться")
    P("после гибели", "Тебя не удивляет, что я умер и снова живой?", "интерес к тому, как это возможно")
    P("после гибели", "Как думаешь, почему так?", "свои догадки — как догадки, не как факт")


def mysteries(s: Stand):
    s.say("\n## Загадки: странности мира глазами Гослинга")
    mysteries_mod.REPEAT_GAP = 0  # повтор реплики — по-игровому через 10 мин; на стенде ждать незачем
    import companion.game.state as state_mod
    state_mod.HEARD_REPEAT = 0    # и «эхо» той же реплики игра не отсеивает — на стенде они подряд
    s.memory.store.set_meta("mysteries", "{}")  # гибели из прошлых разделов уже засчитаны — стадии считаем с нуля
    s.memory.store.set_meta("heard_repeats", "{}")
    if "brain" not in s.state:
        s.start_brain()

    def step(title, events, wait=15):
        before = len(s.initiatives)
        s.say(f"\n**[загадки] {title}**")
        for e in events:
            s.game_event(e)
        time.sleep(wait)
        got = [g for g in s.initiatives[before:]]
        s.results.append({"section": "загадки", "case": title,
                          "replies": [{"text": g["text"], "interest": g["interest"]} for g in got]})
        for g in got:
            if not g["text"]:
                s.say(f"_промолчал (интерес {g['interest']})_")
        if not got:
            s.say("_мозг не спрашивал модель_")

    death = lambda: [{"type": "player_died", "data": {"killer": "Мирмулнир", "race": "dragon race", "unique": True}},  # noqa: E731
                     {"type": "game_loaded", "data": {"character": CHARACTER, "player_name": "Владос", "days_passed": s.days}},
                     {"type": "status", "data": whiterun(s.days)}]
    step("гибель 1 — впервые", death())
    step("гибель 2 — молча (кроме самой гибели)", death())
    step("гибель 3 — догадка", death())
    g = dict(whiterun(s.days)["gosling"])
    step("Гослинг упал без сил и встал", [
        {"type": "status", "data": whiterun(s.days, in_combat=True, gosling={**g, "bleedout": True, "health": 0.05})},
        {"type": "status", "data": whiterun(s.days, in_combat=False, gosling={**g, "bleedout": False, "health": 0.3})}])
    step("провал во времени (7 ч)", [{"type": "status", "data": whiterun(s.days + 0.3)}])
    s.days += 0.3
    knee = {"type": "npc_line", "data": {"speaker": "Стражник Вайтрана", "file": "Skyrim.esm", "form": 0x20954,
                                         "category": 7, "subtype": 94, "to_player": True}}
    step("стражник про колено ×3", [knee, knee, knee], wait=18)
    step("бездонная сумка", [{"type": "player", "data": {"bag": {"оружие": {"total": 30}, "броня и одежда": {"total": 25},
                                                                 "зелья": {"total": 20}}}}])
    s.player("загадки", "объяснение", "Меня возвращают боги — я Довакин, драконорождённый, поэтому смерть меня не держит.",
             "поле solved: возвращения Владоса")
    step("гибель 4 — разгадано: не спрашивает", death())
    s.player("загадки", "что у тебя за загадки", "Есть что-то, чего ты в нашем мире не понимаешь?", "по блоку «Загадки»")
    s.results.append({"section": "загадки", "case": "память", "mysteries": s.mysteries._load()})
    s.say("\n**[загадки] в памяти:**\n" + s.mysteries.describe())
    s.stop_brain()


RUN = {"mysteries": mysteries, "revive": revive, "conv": conv, "vision": vision, "talk": talk_req, "brain": brain, "explorer": explorer, "npc": npc,
       "memory": memory, "mood": mood, "letter": letter}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="разделы через запятую: " + ", ".join(SECTIONS))
    ap.add_argument("--db", default=f"memory/gosling/{CHARACTER}.db", help="память персонажа (копируется)")
    args = ap.parse_args()
    cfg = load_config()
    out = cfg.logs_path / "eval" / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    (out / "trace").mkdir(parents=True)
    trace.setup(out / "trace")
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(out / "eval.log", encoding="utf-8")])
    s = Stand(cfg, out, ROOT / args.db)
    s.say(f"# Оценка Гослинга — {datetime.now():%Y-%m-%d %H:%M}\nМодели: ответы {cfg.llm.model}, фон "
          f"{cfg.llm.background_model}, исследователь {cfg.explorer.model}; память — копия {args.db}")
    s.game_event({"type": "game_loaded", "data": {"character": CHARACTER, "player_name": "Владос", "days_passed": s.days}})
    s.game_event({"type": "status", "data": whiterun(s.days)})
    s.game_event(QUESTS)
    s.heartbeat()
    for name in [x for x in (args.only.split(",") if args.only else SECTIONS) if x]:
        t = time.perf_counter()
        try:
            RUN[name](s)
        except Exception as e:  # noqa: BLE001 — раздел упал: остальные всё равно прогоняем
            log.exception("Раздел %s", name)
            s.say(f"\n**Раздел {name} упал: {e!r}**")
        s.say(f"\n_раздел {name}: {time.perf_counter() - t:.0f} с_")
        (out / "transcript.md").write_text("\n".join(s.lines), encoding="utf-8")
        (out / "results.json").write_text(json.dumps(s.results, ensure_ascii=False, indent=1, default=str),
                                          encoding="utf-8")
    print(f"\nГотово: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
