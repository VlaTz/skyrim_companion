"""HTTP/WebSocket API голосового сервиса.

    GET  /health          — статус и загруженные модели
    POST /stt             — тело: аудиофайл (wav/flac/ogg) -> {"text": ...}
    POST /tts             — {"text", "emotion"?, "play"?} -> audio/wav
    POST /chat            — {"text", "game_context"?, "play"?} -> {"text", "emotion"} (ответ компаньона)
    POST /game/event      — событие из игры {"type", "data"} (см. companion/game/state.py)
    GET  /game/state      — что сервис знает об игре + текст для LLM
    GET  /game/commands   — команды плагину (сказать реплику, …); держит запрос до ?wait= секунд
    WS   /events          — поток событий (речь игрока, ответы компаньона, события игры)
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from contextlib import asynccontextmanager

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError

from companion.brain import Brain
from companion.config import ROOT, Config
from companion.emotions import EMOTIONS
from companion.explorer import Explorer
from companion.game.voices import VoiceSamples
from companion.memory.soul import soul_block
from companion.persona import manner
from companion.talk import Talk
from companion.game.books import BookLibrary
from companion.game.dialogue import DialogueTexts
from companion.game.places import PlaceBook
from companion.game.profile import PlayerProfile
from companion.game.state import Change, GameEvent, GameState
from companion.jokes import JokeBook, Jokes, JokeWriter
from companion.game.items import ItemBook
from companion.knowledge import Knowledge
from companion.letters import Letters
from companion.memory.soul import Soul
from companion.mood import Mood
from companion.mysteries import Mysteries
from companion.orders import Orders
from companion.personal import Personal
from companion.musings import Musings
from companion.memory.diarist import Diarist
from companion.memory.keeper import MemoryKeeper
from companion.memory.thinker import Thinker
from companion.memory.store import MemoryStore
from companion.tools import build_tools
from companion.vision import Vision
from companion.voice.npc_voice import NpcVoice
from companion.voice.service import VoiceService

log = logging.getLogger(__name__)


class TTSRequest(BaseModel):
    text: str
    emotion: str | None = None  # код из companion/emotions.py; None — neutral
    play: bool = False          # озвучить ещё и в динамики сервиса


class ChatRequest(BaseModel):
    text: str                   # реплика игрока
    game_context: str = ""      # что происходит в игре; пусто — из /game/event
    play: bool = True           # озвучить ответ в динамики сервиса


class EventHub:
    """Раздаёт события из рабочих потоков всем подключённым WebSocket-клиентам."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queues: set[asyncio.Queue] = set()

    def publish(self, event: dict) -> None:  # вызывается из любого потока
        log.info("событие: %s", event)
        if self.loop:
            self.loop.call_soon_threadsafe(self._fanout, event)

    def _fanout(self, event: dict) -> None:
        for q in self.queues:
            q.put_nowait(event)


class CommandQueue:
    """Команды плагину. Плагин держит «долгий» GET /game/commands, сервис отвечает, как только
    появилась команда, — задержка почти нулевая без открытого порта в самой игре."""

    STALE = 5.0  # с: команда, которую плагин так и не забрал, к этому времени уже неактуальна

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue | None = None
        self.polling = 0           # сколько запросов плагина сейчас ждут
        self.last_poll = 0.0

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop, self.queue = loop, asyncio.Queue()

    def send(self, command: dict) -> None:  # из любого потока
        if self.loop and self.queue:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, (time.monotonic(), command))

    def connected(self) -> bool:
        return self.polling > 0 or time.monotonic() - self.last_poll < 30

    async def take(self, wait: float) -> list[dict]:
        self.polling += 1
        try:
            items = [await asyncio.wait_for(self.queue.get(), timeout=wait)]
            while not self.queue.empty():
                items.append(self.queue.get_nowait())
        except asyncio.TimeoutError:
            return []
        finally:
            self.polling -= 1
            self.last_poll = time.monotonic()
        now = time.monotonic()
        return [command for ts, command in items if now - ts < self.STALE]


def create_app(cfg: Config) -> FastAPI:
    hub = EventHub()
    books = BookLibrary(cfg.game.data_dir)  # тексты книг игры: прочитанное Владосом и Гослингом
    game = GameState(cfg.map, cfg.game.interface_bsa, dialogue=DialogueTexts(cfg.game.data_dir),
                     profile=PlayerProfile(cfg.game.data_dir), books=books)
    game.hear_distance = cfg.npc_voice.max_distance  # дальше — не слышит, как и Владоса
    commands = CommandQueue()
    places = PlaceBook(cfg.game.data_dir)
    knowledge = Knowledge(cfg.game.data_dir, places, books)  # что Гослинг знает о мире — из файлов игры
    memory = MemoryKeeper(MemoryStore(cfg.memory_path), game, places)
    memory.knowledge = knowledge
    # Настроение — из событий игры, в обстановку; душа усиливает то, что задевает лично.
    mood = Mood(soul=lambda: memory.store.soul_feel() if memory.store.is_open else []) if cfg.mood.enabled else None
    memory.mood = mood
    personal = Personal(mood, cfg.personal) if mood and cfg.personal.enabled else None  # событие задело душу — повод
    # Загадки: «швы» игры глазами Гослинга (Владос погиб — и жив, время провалилось) — удивиться, гадать, спросить.
    mysteries = memory.mysteries = Mysteries(memory.store, cfg.mysteries)
    game.meet = memory.meet  # встречи (раса врага, Крик) — «впервые» или «уже встречали»
    npc_voice = NpcVoice(cfg.npc_voice, game, commands.send, commands.connected,
                         lip_generator=cfg.game.lip_generator if cfg.npc_voice.lip else None)
    orders = Orders(commands.send, commands.connected, game)  # «жди», «пошли» — поле "do" ответа -> игре
    letters = Letters(commands.send, commands.connected, game, memory.letter_context)  # переписка, когда он далеко
    state: dict = {}

    def publish(event: dict) -> None:
        """События голосового сервиса — в журнал WebSocket и в память (реплики игрока и Гослинга)."""
        hub.publish(event)
        memory.on_voice_event(event)
        if event.get("type") == "player_speech" and "explorer" in state:  # слова Владоса — в решения вылазки
            state["explorer"].heard(str(event.get("text") or ""))
        if event.get("type") == "companion_reply" and event.get("do"):  # согласился — делает, пока говорит
            orders.give(str(event["do"]), place=str(event.get("place") or ""), back=bool(event.get("back")),
                        said=str(event.get("text") or ""), who=str(event.get("who") or ""),
                        opener=str(event.get("opener") or ""))
        if event.get("solved"):  # Владос объяснил загадку — больше не спрашивать
            mysteries.solve(*str(event["solved"]).partition(":")[::2])
        if event.get("joke") and "brain" in state and state["brain"].jokes:  # обыграл заготовку — помним, что сказано
            state["brain"].jokes.said(str(event["joke"]))
        if event.get("type") == "companion_reply" and "thinker" in state:
            # высказал мысль (хоть в ответе Владосу) — больше не предлагать; метку модели проверяем по тексту реплики
            for idea in state["thinker"].spoken(event.get("idea"), str(event.get("text") or ""), memory.shown_ideas):
                state["thinker"].said(idea)

    def order_changes(changes: list[Change]) -> None:
        """События не из игры — приказ не вышел ещё до неё (места нет на карте), записи исследователя: как события
        игры — в «Недавно», в память, в настроение, мозгу."""
        game.note(changes)
        memory.record_changes(changes)
        if mood:
            mood.notify(changes)
        # Задело запись души (нашёл неизведанное — мечта картографа) — как и событие игры: повод сказать от себя.
        changes = changes + (personal.notify(changes) if personal else [])
        if "brain" in state:
            state["brain"].notify(changes)

    orders.on_changes = order_changes

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.loop = asyncio.get_running_loop()
        commands.start(hub.loop)
        service = await run_in_threadpool(
            lambda: VoiceService(cfg, publish, game_context=memory.game_context, npc_voice=npc_voice,
                                 memory_context=memory.memory_context, in_combat=lambda: game.in_combat,
                                 mood_emotion=mood.emotion if mood and cfg.mood.voice_default else None,
                                 on_felt=mood.felt if mood else None))
        service.letters = letters
        service.start_push_to_talk()
        state["voice"] = service
        if service.llm:
            vision = None
            if cfg.vision.enabled:  # зрение: кадр игры — «глазам»; описание — в обстановку, по просьбе — look
                vision = Vision(cfg.vision, game, service.llm.client, cfg.logs_path / "frames", model=cfg.llm.model,
                                extra_body=cfg.llm.extra_body, last_activity=lambda: service.last_activity)
                memory.vision = state["vision"] = vision
                vision.start()
            service.llm.set_tools(build_tools(game, places, knowledge, memory, vision))
            asyncio.get_running_loop().run_in_executor(None, knowledge.search, "Скайрим")  # собрать заранее, ~1 с
            llm = service.llm
            llm.events_since = game.events_since
            memory.on_open = lambda: llm.restore(memory.store.last_dialogue(cfg.llm.history_turns))
            soul = (Soul(memory.store, llm.complete_json, cfg.character_path / "prompt.md", cfg.soul,
                         items=ItemBook(cfg.game.data_dir))
                    if cfg.soul.enabled else None)  # что Гослинг решил о себе — после каждой главы; предметы — из игры
            diarist = Diarist(memory.store, llm.complete_json, cfg.diarist,
                              player_name=lambda: memory.store.player_name, knowledge=knowledge, soul=soul,
                              paused=lambda: game.apart_since is not None)
            memory.on_place_changed = diarist.place_changed
            diarist.start()
        if cfg.brain.enabled and service.llm:
            state["brain"] = Brain(cfg.brain, service, game)
            if cfg.jokes.enabled and (jokes_file := cfg.character_path / "jokes.yaml").is_file():  # шутки к месту
                book = JokeBook(jokes_file, cfg.character_path / "jokes_auto.yaml")
                writer = JokeWriter(book, service.llm.complete_json, cfg.jokes.model) if cfg.jokes.refill else None
                state["brain"].jokes = Jokes(book, game, places, memory, writer, cfg.jokes)
            if cfg.brain.musings.enabled:  # размышления о мире в тишине — темы из базы знаний
                state["brain"].musings = Musings(knowledge, memory, cfg.brain.musings.local)
            state["brain"].start()
        if cfg.thinker.enabled and service.llm:  # мысли Гослинга: что держать в голове и предложить
            thinker = Thinker(memory, game, service.llm.complete_json,
                              # и исследователю: мысль в вылазке — его решение (companion/explorer.py, _thought)
                              on_thought=lambda changes: [sink(changes) for sink in (
                                  state["brain"].notify if "brain" in state else None,
                                  state["explorer"].notify if "explorer" in state else None) if sink],
                              cfg=cfg.thinker)
            if "brain" in state:  # мысли «при случае» — мозгу на долгую тишину
                state["brain"].unsaid_thought, state["brain"].thought_said = thinker.unsaid, thinker.said
            state["thinker"] = thinker
            thinker.start()
        prompt_file = cfg.character_path / "prompt.md"

        def npc_said(name: str, text: str) -> str:
            """Слова NPC Гослингу — как событие: в настроение и в проверку «задело лично» (душа). В журнал — нет: там
            они уже «X сказал» (MemoryKeeper, npc_speech). -> что задело ("" — ничего)."""
            change = Change("npc_talk", f"{name} сказал тебе: «{text}»", subject=f"{name}: {text[:40]}")
            if mood:
                mood.notify([change])
            return "; ".join(c.text for c in (personal.notify([change]) if personal else []))

        talk = None
        if cfg.talk.enabled and service.llm:  # разговоры Гослинга с NPC: ответ NPC — его роль по данным игры, его голос
            samples = VoiceSamples(cfg.game.data_dir, cfg.talk.voices_bsa, ROOT / "data" / "npc_voices", cfg.talk.refs)
            talk = Talk(cfg.talk, game, service.llm.complete_json, deliver=service.deliver, say_as=service.say_as,
                        samples=samples, quotes_path=ROOT / "datasets" / "game_quotes.tsv",
                        people=lambda name: "; ".join(text for kind, text in knowledge.search(name, 4)
                                                      if kind == "person" and name.split()[0] in text),
                        model=cfg.explorer.model, manner=manner(prompt_file, without=("Игрока зовут",)),
                        me=lambda: "\n".join(x for x in (
                            soul_block(memory.store) if memory.store.is_open else "", mood.describe() if mood else "")
                            if x),
                        feel=npc_said, place_card=lambda: places.card(game.status.location or ""),
                        evidence=lambda text: [t for kind, t in knowledge.search(text, 4) if not kind.startswith("book")])
        if cfg.explorer.enabled and service.llm:  # «отпусти исследовать» и «веди» — сам решает, куда идти
            explorer = Explorer(cfg.explorer, game, orders, service.llm.complete_json, places, memory.explorer_memory,
                                deliver=lambda text: service.deliver(text, letter=not service.can_speak()),
                                record=order_changes, mood=mood.describe if mood else lambda: "",
                                thoughts=lambda: memory.store.ideas() if memory.store.is_open else [],
                                learn=memory.learn, talk=talk, manner=manner(prompt_file),
                                facts=memory.place_facts)
            state["explorer"] = explorer
            if "brain" in state:  # идёт вылазка — о месте и тишине говорит исследователь, не мозг
                state["brain"].exploring = lambda: explorer.outing.mode if explorer.outing else ""
                state["brain"].outing_hint = explorer.hint  # важное из услышанного — исследователю
            explorer.run()
        log.info("Сервис готов: http://%s:%d", cfg.server.host, cfg.server.port)
        yield
        if "brain" in state:
            state["brain"].stop()
        if "vision" in state:
            state["vision"].stop()
        service.stop()

    app = FastAPI(title="Skyrim Companion — voice", lifespan=lifespan)

    def voice() -> VoiceService:
        return state["voice"]

    @app.get("/health")
    def health():
        v = voice()
        return {
            "status": "ok",
            "character": cfg.character,
            "stt": f"{cfg.stt.model} ({cfg.stt.device})",
            "tts": cfg.tts.model,
            "llm": f"{cfg.llm.model} @ {cfg.llm.base_url}" if v.llm else None,
            "emotions": list(EMOTIONS),
            "push_to_talk": cfg.push_to_talk.key if v.recorder else None,
        }

    @app.post("/stt")
    async def stt(request: Request):
        body = await request.body()
        try:
            audio, sr = sf.read(io.BytesIO(body), dtype="float32")
        except Exception as e:
            raise HTTPException(400, f"Не удалось прочитать аудио: {e}")
        text = await run_in_threadpool(voice().transcribe, audio, sr)
        return {"text": text}

    @app.post("/tts")
    async def tts(req: TTSRequest):
        if not req.text.strip():
            raise HTTPException(400, "Пустой текст")
        if req.emotion and req.emotion not in EMOTIONS:
            raise HTTPException(400, f"Неизвестная эмоция {req.emotion!r}, есть: {list(EMOTIONS)}")
        audio, sr = await run_in_threadpool(voice().synthesize, req.text, req.emotion)
        if req.play:
            asyncio.get_running_loop().run_in_executor(None, voice().play, audio, sr)
        buf = io.BytesIO()
        sf.write(buf, np.clip(audio, -1, 1), sr, format="WAV", subtype="PCM_16")
        return Response(buf.getvalue(), media_type="audio/wav")

    @app.post("/chat")
    async def chat(req: ChatRequest):
        if not req.text.strip():
            raise HTTPException(400, "Пустой текст")
        try:
            reply = await run_in_threadpool(voice().respond, req.text, req.game_context)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # сервер LLM не запущен, неверная модель, таймаут ...
            log.exception("Ошибка LLM")
            raise HTTPException(502, f"LLM недоступна ({cfg.llm.base_url}): {e}")
        hub.publish({"type": "companion_reply", "text": reply.text, "emotion": reply.emotion})
        if req.play:
            asyncio.get_running_loop().run_in_executor(None, voice().speak, reply.text, reply.emotion)
        return {"text": reply.text, "emotion": reply.emotion}

    @app.post("/game/event")
    def game_event(event: GameEvent):
        try:
            changes = game.apply(event)
        except ValidationError as e:
            raise HTTPException(422, f"Неверные поля {event.type}: {e}")
        memory.on_game_event(event, changes)
        # Чего Гослинг не видел (он не рядом), то его не трогает и не повод говорить — только в журнал памяти.
        seen = [c for c in changes if not c.unseen]
        if mood and seen:
            mood.notify(seen)
        touched = personal.notify(seen) if personal and seen else []  # задело запись души — повод мозгу: от себя
        seen += touched
        if odd := mysteries.on_event(event.type, event.data, changes, game.player):  # странность — пора сказать о ней
            memory.record_changes(odd)
            seen += odd
            touched += odd
        if "vision" in state and seen:  # новое место, смена погоды — взглянуть (в фоне)
            state["vision"].notify(seen)
        texts = [c.text for c in changes + touched]
        if texts:  # служебные (карта, квесты, сведения об игроке, say_result) в журнал не пишем
            hub.publish({"type": "game_event", "event": event.type, "changes": texts})
        if "brain" in state and seen:
            state["brain"].notify(seen)
        if "explorer" in state and seen:  # его драка, рана в вылазке; встреча — рассказать и отдать находки
            state["explorer"].notify(seen)
        return {"changes": texts}

    @app.get("/game/state")
    def game_state():
        return {**game.snapshot(), "context": memory.game_context(), "memory": memory.memory_context(),
                "player": game.player_details()}

    @app.get("/game/commands")
    async def game_commands(wait: float = 20):
        return {"commands": await commands.take(min(wait, 25))}

    @app.websocket("/events")
    async def events(ws: WebSocket):
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue()
        hub.queues.add(q)
        try:
            while True:
                await ws.send_json(await q.get())
        except WebSocketDisconnect:
            pass
        finally:
            hub.queues.discard(q)

    return app
