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
from companion.config import Config
from companion.emotions import EMOTIONS
from companion.game.books import BookLibrary
from companion.game.dialogue import DialogueTexts
from companion.game.places import PlaceBook
from companion.game.profile import PlayerProfile
from companion.game.state import GameEvent, GameState
from companion.jokes import JokeBook, Jokes, JokeWriter
from companion.game.items import ItemBook
from companion.knowledge import Knowledge
from companion.memory.soul import Soul
from companion.mood import Mood
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
    commands = CommandQueue()
    places = PlaceBook(cfg.game.data_dir)
    knowledge = Knowledge(cfg.game.data_dir, places, books)  # что Гослинг знает о мире — из файлов игры
    memory = MemoryKeeper(MemoryStore(cfg.memory_path), game, places)
    memory.knowledge = knowledge
    # Настроение — из событий игры, в обстановку; душа усиливает то, что задевает лично.
    mood = Mood(soul=lambda: memory.store.soul_feel() if memory.store.is_open else []) if cfg.mood.enabled else None
    memory.mood = mood
    game.meet = memory.meet  # встречи (раса врага, Крик) — «впервые» или «уже встречали»
    npc_voice = NpcVoice(cfg.npc_voice, game, commands.send, commands.connected,
                         lip_generator=cfg.game.lip_generator if cfg.npc_voice.lip else None)
    state: dict = {}

    def publish(event: dict) -> None:
        """События голосового сервиса — в журнал WebSocket и в память (реплики игрока и Гослинга)."""
        hub.publish(event)
        memory.on_voice_event(event)
        if event.get("joke") and "brain" in state and state["brain"].jokes:  # обыграл заготовку — помним, что сказано
            state["brain"].jokes.said(str(event["joke"]))
        if event.get("idea") and "thinker" in state:  # высказал мысль (хоть в ответе Владосу) — больше не предлагать
            if idea := memory.shown_ideas.get(str(event["idea"]).strip("[] ")):
                state["thinker"].said(idea)
            else:
                log.info("Мысль «%s»: такой метки в показанных нет — не помечаем", event["idea"])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.loop = asyncio.get_running_loop()
        commands.start(hub.loop)
        service = await run_in_threadpool(
            lambda: VoiceService(cfg, publish, game_context=memory.game_context, npc_voice=npc_voice,
                                 memory_context=memory.memory_context, in_combat=lambda: game.in_combat,
                                 mood_emotion=mood.emotion if mood and cfg.mood.voice_default else None,
                                 on_felt=mood.felt if mood else None))
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
                              player_name=lambda: memory.store.player_name, knowledge=knowledge, soul=soul)
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
                              on_thought=state["brain"].notify if "brain" in state else lambda changes: None,
                              cfg=cfg.thinker)
            if "brain" in state:  # мысли «при случае» — мозгу на долгую тишину
                state["brain"].unsaid_thought, state["brain"].thought_said = thinker.unsaid, thinker.said
            state["thinker"] = thinker
            thinker.start()
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
        if mood and changes:
            mood.notify(changes)
        if "vision" in state and changes:  # новое место, смена погоды — взглянуть (в фоне)
            state["vision"].notify(changes)
        texts = [c.text for c in changes]
        if changes:  # служебные (карта, квесты, сведения об игроке, say_result) в журнал не пишем
            hub.publish({"type": "game_event", "event": event.type, "changes": texts})
        if "brain" in state and changes:
            state["brain"].notify(changes)
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
