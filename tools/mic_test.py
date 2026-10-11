
"""Проверка голосового тракта с микрофоном, без LLM: говоришь -> Whisper распознаёт ->
персонаж повторяет фразу своим голосом с выбранной эмоцией.

    python tools/mic_test.py                     # Enter — начать запись, Enter — закончить
    python tools/mic_test.py --ptt               # держать F8 (клавиша из config.yaml)
    python tools/mic_test.py --emotion anger     # эмоция на старте
    python tools/mic_test.py --devices           # список аудиоустройств
    python tools/mic_test.py --input 1 --output "AirPods"
    python tools/mic_test.py --save              # сохранять записи в recordings/

Эмоция: перед записью набери номер или название (3, anger, злость) и Enter.
Выход — q и Enter, или Ctrl+C.
"""

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion.config import ROOT, load_config  # noqa: E402
from companion.emotions import DEFAULT_EMOTION, EMOTIONS, emotions_hint, parse_emotion  # noqa: E402
from companion.voice.audio_io import PushToTalkRecorder, resolve_device  # noqa: E402
from companion.voice.service import VoiceService  # noqa: E402

OUT_DIR = ROOT / "recordings"
QUIT = ("q", "quit", "exit", "й")


def parse_device(value: str | None) -> int | str | None:
    if value is None:
        return None
    return int(value) if value.isdigit() else value


def record_until_enter(sample_rate: int, device: int | str | None) -> np.ndarray:
    chunks: list[np.ndarray] = []

    def callback(indata, frames, time_info, status):
        chunks.append(indata[:, 0].copy())

    with sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32",
                        device=resolve_device(device, "input"), callback=callback):
        input("● Запись... говори, затем нажми Enter")
    return np.concatenate(chunks) if chunks else np.zeros(0, np.float32)


class Tester:
    def __init__(self, service: VoiceService, save: bool, emotion: str):
        self.service = service
        self.save = save
        self.emotion = emotion
        self.n = 0
        self.busy = threading.Lock()

    def show_emotions(self) -> None:
        print(f"Эмоции: {emotions_hint()}")
        print(f"Сейчас: {self.emotion} ({EMOTIONS[self.emotion]})")

    def set_emotion(self, value: str) -> bool:
        emotion = parse_emotion(value)
        if emotion is None:
            print(f"  Не знаю эмоцию «{value}».")
            self.show_emotions()
            return False
        self.emotion = emotion
        print(f"  Эмоция: {emotion} ({EMOTIONS[emotion]})")
        return True

    def handle(self, audio: np.ndarray) -> None:
        with self.busy:
            sr = self.service.cfg.audio.sample_rate
            duration = len(audio) / sr
            peak = float(np.abs(audio).max()) if len(audio) else 0.0
            print(f"■ Записано {duration:.1f} с, пик громкости {peak:.2f}")
            if peak < 0.01:
                print("  ⚠ Почти тишина — проверь микрофон (--devices, --input)")

            t0 = time.perf_counter()
            text = self.service.transcribe(audio, sr)
            t1 = time.perf_counter()
            if not text:
                print("  (ничего не распознано)\n")
                return
            print(f"  Ты сказал:  «{text}»   [STT {t1 - t0:.2f} с]")

            emotion = self.emotion
            speech = self.service.speak(text, emotion)
            t2 = time.perf_counter()
            print(f"  Озвучка:    {len(speech.audio) / speech.sample_rate:.1f} с аудио, эмоция {emotion} "
                  f"[TTS до первого звука {speech.first_audio:.2f} с]")
            print(f"  От конца записи до начала ответа: {t1 - t0 + speech.first_audio:.2f} с")

            if self.save:
                self.n += 1
                OUT_DIR.mkdir(exist_ok=True)
                stamp = time.strftime("%H%M%S")
                sf.write(OUT_DIR / f"{stamp}_{self.n:02d}_in.wav", audio, sr)
                sf.write(OUT_DIR / f"{stamp}_{self.n:02d}_out_{emotion}.wav", speech.audio, speech.sample_rate)
            print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ptt", action="store_true", help="push-to-talk клавишей из config.yaml")
    ap.add_argument("--devices", action="store_true", help="показать аудиоустройства и выйти")
    ap.add_argument("--input", help="микрофон: номер или часть имени")
    ap.add_argument("--output", help="динамики: номер или часть имени")
    ap.add_argument("--emotion", default=DEFAULT_EMOTION, help="эмоция на старте: " + ", ".join(EMOTIONS))
    ap.add_argument("--character", help="персонаж (папка в characters/)")
    ap.add_argument("--save", action="store_true", help="сохранять записи в recordings/")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if args.devices:
        print(sd.query_devices())
        print("\n> — вход по умолчанию, < — выход по умолчанию")
        return 0

    emotion = parse_emotion(args.emotion)
    if emotion is None:
        print(f"Неизвестная эмоция {args.emotion!r}. Есть: {emotions_hint()}")
        return 1

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(levelname).1s %(name)s: %(message)s")

    cfg = load_config()
    cfg.push_to_talk.enabled = False  # сервисный push-to-talk не нужен, управляем сами
    if args.input is not None:
        cfg.audio.input_device = parse_device(args.input)
    if args.output is not None:
        cfg.audio.output_device = parse_device(args.output)
    if args.character:
        cfg.character = args.character

    in_dev = resolve_device(cfg.audio.input_device, "input")
    out_dev = resolve_device(cfg.audio.output_device, "output")
    print(f"Микрофон: {sd.query_devices(in_dev, 'input')['name']}")
    print(f"Динамики: {sd.query_devices(out_dev, 'output')['name']}")
    print(f"Whisper: {cfg.stt.model} ({cfg.stt.device}) | XTTS {cfg.tts.model}, "
          f"голос: characters/{cfg.character}/voice")
    print("Загрузка моделей...")
    service = VoiceService(cfg, use_llm=False)
    tester = Tester(service, args.save, emotion)
    print("Готово.\n")

    try:
        if args.ptt:
            recorder = PushToTalkRecorder(cfg.push_to_talk, cfg.audio, tester.handle)
            recorder.start()
            print(f"Держи {cfg.push_to_talk.key.upper()} и говори. "
                  "Сменить эмоцию — номер/название и Enter. q — выход.")
            tester.show_emotions()
            print()
            while True:
                cmd = input().strip().lower()
                if cmd in QUIT:
                    break
                if cmd:
                    tester.set_emotion(cmd)
        else:
            while True:
                tester.show_emotions()
                cmd = input("Enter — запись | номер/название — сменить эмоцию и записать | q — выход: ")
                cmd = cmd.strip().lower()
                if cmd in QUIT:
                    break
                if cmd and not tester.set_emotion(cmd):
                    continue
                tester.handle(record_until_enter(cfg.audio.sample_rate, cfg.audio.input_device))
    except (KeyboardInterrupt, EOFError):
        pass
    print("Пока!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
