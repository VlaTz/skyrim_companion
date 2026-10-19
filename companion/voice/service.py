"""Компаньон целиком: микрофон -> Whisper -> LLM -> TTS (голос + эмоция) -> динамики.
TTS — XTTS на видеокарте или облачный (tts.engine в config.yaml).

С echo: true в config.yaml LLM пропускается — распознанная фраза озвучивается обратно
(проверка голосового тракта).
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np
import soxr

from companion import trace
from companion.config import Config
from companion.emotions import DEFAULT_EMOTION
from companion.voice.audio_io import PushToTalkRecorder, play, play_stream
from companion.voice.npc_voice import NpcVoice
from companion.voice.stt import WHISPER_SR, SpeechToText
from companion.voice.xtts import XttsTTS, split_text

log = logging.getLogger(__name__)

EventSink = Callable[[dict], None]
ContextSource = Callable[[], str]


@dataclass
class Speech:
    audio: np.ndarray
    sample_rate: int
    first_audio: float  # секунд от вызова до начала звука


class VoiceService:
    def __init__(self, cfg: Config, on_event: EventSink | None = None, use_llm: bool | None = None,
                 game_context: ContextSource | None = None, npc_voice: NpcVoice | None = None,
                 memory_context: ContextSource | None = None, in_combat: Callable[[], bool] | None = None,
                 mood_emotion: Callable[[], str | None] | None = None,
                 on_felt: Callable[[str, int, str], None] | None = None):
        self.cfg = cfg
        self.npc_voice = npc_voice  # голос из уст Гослинга в игре; None — только колонки
        self.on_event = on_event or (lambda e: None)
        self.game_context = game_context or (lambda: "")  # что сейчас в игре — текст для LLM
        self.memory_context = memory_context or (lambda: "")  # что Гослинг помнит — {memory} промпта
        self.in_combat = in_combat or (lambda: False)  # идёт бой — отвечать в пару слов
        self.mood_emotion = mood_emotion or (lambda: None)  # эмоция настроения — голосу, если модель нейтральна
        self.on_felt = on_felt or (lambda feeling, strength, said: None)  # реплика игрока задела — настроению
        models = cfg.models_path

        self.stt = SpeechToText(cfg.stt, models)
        if cfg.tts.engine == "cloud":
            from companion.voice.cloud_tts import CloudTTS
            self.tts = CloudTTS(cfg.tts.cloud, fallback_key=cfg.llm.api_key or (
                os.environ.get(cfg.llm.api_key_env, "") if cfg.llm.api_key_env else ""),
                voice_dir=cfg.character_path / "voice")
        else:
            self.tts = XttsTTS(cfg.tts, models, cfg.character_path / "voice")

        use_llm = (not cfg.echo) if use_llm is None else use_llm
        self.llm = None
        if use_llm:
            from companion.llm import CompanionLLM
            self.llm = CompanionLLM(cfg.llm, cfg.character_path / "prompt.md")

        # Модели не потокобезопасны, а реплики не должны накладываться друг на друга.
        self._stt_lock = threading.Lock()
        self._tts_lock = threading.Lock()
        self._play_lock = threading.Lock()
        self._turn = threading.Lock()        # один ход разговора (ответ + озвучка) за раз
        # Игрок заговорил (нажал F8) — у его речи приоритет: Гослинг бросает свою реплику (следующие фразы,
        # звук в колонках, ещё не сказанный ответ модели). Снимается, когда начинается ход игрока.
        self._player_speaking = threading.Event()
        self.last_activity = time.time()     # когда последний раз кто-то говорил
        self.last_joke_at = 0.0              # когда Гослинг последний раз пошутил (мозгу: brain.quip_gap)
        self.recorder: PushToTalkRecorder | None = None

    # ------------------------------------------------------------ API

    def transcribe(self, audio: np.ndarray, sample_rate: int = WHISPER_SR) -> str:
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sample_rate != WHISPER_SR:
            audio = soxr.resample(audio, sample_rate, WHISPER_SR)
        with self._stt_lock:
            return self.stt.transcribe(audio)

    def synthesize(self, text: str, emotion: str | None = None) -> tuple[np.ndarray, int]:
        """Аудио целиком."""
        with self._tts_lock:
            return self.tts.synthesize(text, emotion)

    def voiced(self, emotion: str | None) -> str | None:
        """С какой эмоцией звучать: модель ответила нейтрально, а настроение сильное — эмоция настроения."""
        if (emotion or DEFAULT_EMOTION) == DEFAULT_EMOTION and (mood := self.mood_emotion()):
            trace.event(f"Голос: {mood} — по настроению (модель: {emotion or DEFAULT_EMOTION})")
            return mood
        return emotion

    def interrupted(self) -> bool:
        """Игрок заговорил — Гослингу пора замолчать."""
        return self._player_speaking.is_set()

    def speakers(self) -> bool:
        """Можно ли говорить в колонки: голоса из игры нет вовсе (npc_voice выключен) или так задано
        (npc_voice.speakers). Иначе Гослинг говорит только в игре."""
        return not (self.npc_voice and self.cfg.npc_voice.enabled) or self.cfg.npc_voice.speakers

    def can_speak(self) -> bool:
        """Гослинга сейчас услышат: голосом из игры (рядом, игра на связи) или в колонки, если они разрешены.
        Нет — и спрашивать модель незачем: ни реплику сам, ни ответ (он далеко — не слышит Владоса)."""
        return self.speakers() or bool(self.npc_voice and self.npc_voice.available())

    def speak(self, text: str, emotion: str | None = None) -> Speech:
        """Голосом Гослинга из игры, если он рядом (npc_voice); иначе — в колонки, если они разрешены
        (speakers), или молча. Игрок заговорил — замолкает (фразу, уже звучащую в игре, договорит)."""
        tone = emotion or DEFAULT_EMOTION  # с какой эмоцией звучит — в трассу
        silent = Speech(np.zeros(0, np.float32), self.tts.sample_rate, 0.0)
        if self.npc_voice and not self.speakers() and not self.npc_voice.available():
            trace.event(f"Не озвучено: Гослинг не рядом или игра не на связи («{text[:60]}»)")
            return silent
        if self.npc_voice and self.npc_voice.available():
            pieces = split_text(text, min(self.tts.char_limit, self.cfg.npc_voice.max_chars),
                                first_limit=self.cfg.npc_voice.first_chars)
            said, first, ready = self.npc_voice.speak(pieces, lambda piece: self.synthesize(piece, emotion),
                                                      should_stop=self.interrupted)
            if said == len(pieces) or self.interrupted():
                if said < len(pieces):
                    log.info("Игрок перебил — Гослинг не договорил %d из %d фраз", len(pieces) - said, len(pieces))
                    trace.event(f"Озвучка [{tone}]: из Гослинга {said} из {len(pieces)} фраз — перебил игрок")
                else:
                    trace.event(f"Озвучка [{tone}]: из Гослинга, {len(pieces)} фраз, первая через {first or 0:.2f} с")
                return Speech(np.zeros(0, np.float32), self.tts.sample_rate, first or 0.0)
            if not self.speakers():
                trace.event(f"Озвучка [{tone}]: из Гослинга {said} из {len(pieces)} фраз — в игре не вышло, "
                            "остаток не озвучен (колонки выключены)")
                return Speech(np.zeros(0, np.float32), self.tts.sample_rate, first or 0.0)
            trace.event(f"Озвучка [{tone}]: из Гослинга {said} из {len(pieces)} фраз, остаток — в колонки"
                        + (" (несказанная фраза — готовым звуком)" if ready else ""))
            # Не вышло из игры — остаток в колонки; фразу, на которой не вышло, — готовым звуком, а не синтезом заново.
            rest = pieces[said + 1:] if ready else pieces[said:]
            return self._speak_to_speakers(" ".join(rest), emotion, ready=ready)
        return self._speak_to_speakers(text, emotion)

    def _speak_to_speakers(self, text: str, emotion: str | None = None,
                           ready: tuple[np.ndarray, int] | None = None) -> Speech:
        """В динамики; с tts.stream — потоково, звук начинается до конца синтеза. ready — уже синтезированный
        звук фразы перед text (не вышла голосом из игры): звучит первым, без нового синтеза."""
        trace.event(f"Озвучка [{emotion or DEFAULT_EMOTION}]: в колонки «{text[:80]}{'…' if len(text) > 80 else ''}»")
        t = time.perf_counter()
        first: list[float] = []
        mark = lambda: first.append(time.perf_counter() - t)  # noqa: E731
        device = self.cfg.audio.output_device
        before = [ready[0]] if ready else []

        if self.cfg.tts.stream:
            with self._tts_lock, self._play_lock:
                chunks = itertools.chain(before, self.tts.stream(text, emotion) if text else ())  # синтез — по ходу
                audio = play_stream(chunks, self.tts.sample_rate, device, mark,
                                    prebuffer=self.cfg.tts.stream_prebuffer, should_stop=self.interrupted)
            sr = self.tts.sample_rate
        else:
            audio, sr = self.synthesize(text, emotion) if text else (np.zeros(0, np.float32), self.tts.sample_rate)
            audio = np.concatenate([*before, audio])
            with self._play_lock:
                mark()
                play(audio, sr, device, should_stop=self.interrupted)
        return Speech(audio, sr, first[0] if first else 0.0)

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        with self._play_lock:
            play(audio, sample_rate, self.cfg.audio.output_device)

    def respond(self, player_text: str, game_context: str = ""):
        """Ответ компаньона на реплику игрока (нужна LLM, т.е. echo: false).
        Без game_context берётся текущее состояние игры."""
        if self.llm is None:
            raise RuntimeError("LLM не подключена: в config.yaml echo: true")
        return self.llm.reply(player_text, game_context or self.game_context(), self.memory_context(),
                              in_combat=self.in_combat())

    def busy(self) -> bool:
        """Идёт ход разговора (Гослинг думает или говорит) или игрок сейчас говорит."""
        return self._turn.locked() or self.interrupted()

    def initiate(self, situation: str, min_interest: int | None = None,
                 still_fresh: Callable[[], bool] | None = None, quip_chance: float = 0.0,
                 reasoning: str = "", max_tokens: int | None = None) -> bool:
        """Реплика по своей инициативе (событие в игре, долгая тишина). Не перебивает:
        если сейчас идёт разговор — сразу False. False и если модель решила промолчать."""
        if self.llm is None or self.interrupted() or not self.can_speak() or not self._turn.acquire(blocking=False):
            return False
        try:
            reply = self.llm.initiate(situation, self.game_context(), self.memory_context(),
                                      min_interest=min_interest, quip_chance=quip_chance,
                                      reasoning=reasoning, max_tokens=max_tokens)
            if not reply.text:
                return False
            if still_fresh and not still_fresh():  # пока модель думала, игрок вышел, вошёл, влез в бой
                self.llm.drop_last(reply)
                log.info("Обстановка сменилась — реплику по своей инициативе не говорим")
                trace.event(f"Не сказано: пока думал, обстановка сменилась («{reply.text[:80]}»)")
                return False
            if self.interrupted():  # пока модель думала, игрок заговорил — его очередь
                self.llm.drop_last(reply)
                log.info("Игрок заговорил — реплику по своей инициативе не говорим")
                trace.event("Не сказано: пока думал, заговорил игрок")
                return False
            emotion = self.voiced(reply.emotion)
            self.on_event({"type": "companion_reply", "text": reply.text, "emotion": emotion,
                           "initiative": True, "remember": reply.remember, "joke": reply.joke, "idea": reply.idea})
            self.speak(reply.text, emotion)
            self.last_activity = time.time()  # только если сказал: промолчал — тишина продолжается
            if reply.joke or reply.quip:
                self.last_joke_at = self.last_activity
            return True
        except Exception:
            log.exception("Ошибка реплики по своей инициативе")
            return False
        finally:
            self._turn.release()

    # ------------------------------------------------------------ push-to-talk

    def start_push_to_talk(self) -> None:
        if not self.cfg.push_to_talk.enabled:
            return
        self.recorder = PushToTalkRecorder(self.cfg.push_to_talk, self.cfg.audio, self._on_utterance,
                                           on_start=self._player_speaking.set,
                                           on_skip=self._player_speaking.clear)
        self.recorder.start()

    def stop(self) -> None:
        if self.recorder:
            self.recorder.stop()

    def _on_utterance(self, audio: np.ndarray) -> None:
        if not self.can_speak():  # Гослинг далеко (или игра не на связи): не слышит и не ответит
            log.info("Гослинг не рядом — не слышит Владоса")
            trace.event("Не услышал: Гослинг не рядом или игра не на связи")
            self._player_speaking.clear()
            return
        try:
            t = time.perf_counter()
            text = self.transcribe(audio, self.cfg.audio.sample_rate)
            if not text:
                self._player_speaking.clear()
                return
            self.on_event({"type": "player_speech", "text": text})
            self.last_activity = time.time()
            with self._turn:  # реплика Гослинга, если шла, уже бросается (interrupted)
                self._player_speaking.clear()  # ход игрока: теперь перебить может только новое нажатие F8
                if self.llm is None:
                    reply_text, emotion = text, DEFAULT_EMOTION
                else:
                    reply = self.respond(text)
                    if reply.felt:  # задела ли его реплика игрока — до выбора голоса: «обидел» уже слышно
                        self.on_felt(reply.felt[0], reply.felt[1], text)
                    reply_text, emotion, remember = reply.text, self.voiced(reply.emotion), reply.remember
                    if not reply_text:
                        return
                    if self.interrupted():  # пока думал, игрок заговорил снова — ответ на новое важнее
                        self.llm.drop_last(reply)
                        log.info("Игрок заговорил снова — прежний ответ не говорим")
                        trace.event("Не сказано: игрок заговорил снова")
                        return
                    self.on_event({"type": "companion_reply", "text": reply_text, "emotion": emotion,
                                   "remember": remember, "idea": reply.idea})
                before_tts = time.perf_counter() - t
                speech = self.speak(reply_text, emotion)
            log.info("от конца фразы до начала ответа: %.2f с", before_tts + speech.first_audio)
        except Exception:
            log.exception("Ошибка обработки фразы")
        finally:
            self.last_activity = time.time()
