"""Голос из уст Гослинга-NPC (вместо колонок): как у Mantella.

В Gosling.esp две темы-заглушки. Ответ режем на фразы; каждую синтезируем целиком, кладём в .wav
очередной темы (в папку голоса мода в MO2) и командуем плагину «скажи тему». Темы чередуем: если
сказать одну тему дважды подряд, игра может проиграть старый звук из кэша. Пока звучит фраза,
синтезируем следующую; следующую «скажи» шлём, когда предыдущая доиграла.

Губы: к каждой фразе .lip рядом с .wav — генератором из комплекта Creation Kit (LipGenerator.exe).
Он знает только английский, поэтому текст не даём — он разбирает сам звук (для губ этого хватает).

Если Гослинга рядом нет (или плагин не ответил) — говорим через колонки, как раньше.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf
import soxr

from companion import gosling_mod as mod
from companion.config import NpcVoiceConfig
from companion.game.state import GameState

log = logging.getLogger(__name__)

Synth = Callable[[str], tuple[np.ndarray, int]]
GAME_SAMPLE_RATE = 44100
LIP_MIN_SECONDS = 1.0  # фразы короче генератор .lip не разбирает — добиваем тишиной


class NpcVoice:
    def __init__(self, cfg: NpcVoiceConfig, game: GameState,
                 send_command: Callable[[dict], None], plugin_connected: Callable[[], bool],
                 lip_generator: Path | None = None):
        self.cfg = cfg
        self.game = game
        self.send_command = send_command
        self.plugin_connected = plugin_connected
        self.voice_dir = cfg.voice_dir
        self.next_line = 0
        self._waiting: dict[str, tuple[threading.Event, dict]] = {}
        self._lock = threading.Lock()
        game.on_say_result = self._on_result
        self.lip_generator = lip_generator
        if lip_generator and not lip_generator.is_file():
            log.warning("Нет генератора .lip (%s) — Гослинг будет говорить без движения губ", lip_generator)
            self.lip_generator = None
        # Генератор пишет промежуточный tmp16khz.wav в текущую папку — запускаем его во временной.
        self._lip_workdir = Path(tempfile.gettempdir()) / "skyrim_companion_lip"

    def available(self) -> bool:
        """Можно говорить голосом из игры: включено, мод на месте, плагин на связи, Гослинг рядом."""
        return (self.cfg.enabled and self.voice_dir.is_dir() and self.plugin_connected()
                and self.game.gosling_near(self.cfg.max_distance))

    def speak(self, pieces: list[str], synth: Synth,
              should_stop: Callable[[], bool] = lambda: False) -> tuple[int, float | None]:
        """Произносит фразы по очереди. Возвращает (сколько фраз сказано, через сколько секунд
        прозвучала первая). Сказано меньше, чем фраз: либо Гослинг не смог (остаток — в колонки), либо
        should_stop() — игрок заговорил, остаток не нужен (это решает вызывающий). Фразу, которая уже
        звучит в игре, не оборвать — но следующих не будет и ход разговора освобождается сразу."""
        t0 = time.perf_counter()
        first: float | None = None
        free_at = 0.0  # когда доиграет предыдущая фраза (time.monotonic)
        for i, piece in enumerate(pieces):
            if should_stop():
                return i, first
            audio, sr = synth(piece)
            line = self.next_line
            # Пишем в файл другой темы, пока звучит предыдущая: её файл не трогаем.
            # Частота — как у звуков игры (44,1 кГц), а не XTTS (24 кГц).
            game_audio = soxr.resample(audio, sr, GAME_SAMPLE_RATE) if sr != GAME_SAMPLE_RATE else audio
            if self.lip_generator and len(game_audio) < LIP_MIN_SECONDS * GAME_SAMPLE_RATE:
                game_audio = np.pad(game_audio, (0, int(LIP_MIN_SECONDS * GAME_SAMPLE_RATE) - len(game_audio)))
            wav = self.voice_dir / mod.voice_file(line)
            sf.write(wav, np.clip(game_audio, -1, 1), GAME_SAMPLE_RATE, subtype="PCM_16")
            self._make_lip(wav)
            if not self._wait_until(free_at, should_stop):
                return i, first
            duration = len(game_audio) / GAME_SAMPLE_RATE
            result = self._say(line, piece, duration)
            if not result.get("ok"):
                log.warning("Гослинг не сказал фразу %d: %s — дальше через колонки",
                            i + 1, result.get("reason") or "плагин не ответил")
                return i, first
            self.next_line ^= 1
            if first is None:
                first = time.perf_counter() - t0
            free_at = time.monotonic() + duration + self.cfg.gap
        self._wait_until(free_at, should_stop)  # не отпускаем ход разговора, пока Гослинг не договорил
        return len(pieces), first

    @staticmethod
    def _wait_until(moment: float, should_stop: Callable[[], bool]) -> bool:
        """Ждёт до moment (time.monotonic); False — прервали (игрок заговорил)."""
        while (left := moment - time.monotonic()) > 0:
            if should_stop():
                return False
            time.sleep(min(left, 0.05))
        return not should_stop()

    def _make_lip(self, wav: Path) -> None:
        """.lip рядом с .wav (тот же файл, что лежит в моде с запуска игры, — перезаписываем)."""
        if not self.lip_generator:
            return
        t = time.perf_counter()
        self._lip_workdir.mkdir(exist_ok=True)
        try:
            result = subprocess.run([str(self.lip_generator), str(wav), "", "-Automated"],
                                    cwd=self._lip_workdir, capture_output=True, text=True, timeout=10,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode != 0:
                log.warning("Не вышло сделать .lip (код %d): %s — губы будут от прошлой фразы",
                            result.returncode, (result.stdout + result.stderr).strip()[-300:])
                return
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("Не вышло сделать .lip: %s", e)
            return
        log.debug(".lip за %.2f с", time.perf_counter() - t)

    def _say(self, line: int, text: str, duration: float) -> dict:
        """«Скажи тему line»; text — субтитр (у реплики-заглушки в моде текст «...», плагин подменяет),
        duration — длина фразы, с: столько плагин держит субтитр с этим текстом."""
        command_id = uuid.uuid4().hex[:8]
        done = threading.Event()
        result: dict = {}
        with self._lock:
            self._waiting[command_id] = (done, result)
        t = time.perf_counter()
        self.send_command({"type": "say", "line": line, "id": command_id, "text": text,
                           "duration": round(duration, 2)})
        answered = done.wait(self.cfg.result_timeout)
        with self._lock:
            self._waiting.pop(command_id, None)
        log.info("say %s (тема %d): %s за %.2f с", command_id, line,
                 ("ok" if result.get("ok") else f"отказ: {result.get('reason')}") if answered else "нет ответа",
                 time.perf_counter() - t)
        return result

    def _on_result(self, data: dict) -> None:
        with self._lock:
            waiting = self._waiting.get(str(data.get("id")))
        if not waiting:
            log.warning("Опоздавший ответ плагина: %s", data)
        if waiting:
            done, result = waiting
            result.update(data)
            done.set()
