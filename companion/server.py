"""HTTP/WebSocket API голосового сервиса.

    GET  /health          — статус и загруженные модели
    POST /stt             — тело: аудиофайл (wav/flac/ogg) -> {"text": ...}
    POST /tts             — {"text", "emotion"?, "play"?} -> audio/wav
    POST /chat            — {"text", "game_context"?, "play"?} -> {"text", "emotion"} (ответ компаньона)
    WS   /events          — поток событий (речь игрока, ответы компаньона)
"""

from __future__ import annotations

import asyncio
import io
import logging
from contextlib import asynccontextmanager

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel

from companion.config import Config
from companion.emotions import EMOTIONS
from companion.voice.service import VoiceService

log = logging.getLogger(__name__)


class TTSRequest(BaseModel):
    text: str
    emotion: str | None = None  # код из companion/emotions.py; None — neutral
    play: bool = False          # озвучить ещё и в динамики сервиса


class ChatRequest(BaseModel):
    text: str                   # реплика игрока
    game_context: str = ""      # что происходит в игре (локация, бой, ...)
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


def create_app(cfg: Config) -> FastAPI:
    hub = EventHub()
    state: dict[str, VoiceService] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.loop = asyncio.get_running_loop()
        service = await run_in_threadpool(VoiceService, cfg, hub.publish)
        service.start_push_to_talk()
        state["voice"] = service
        log.info("Сервис готов: http://%s:%d", cfg.server.host, cfg.server.port)
        yield
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
