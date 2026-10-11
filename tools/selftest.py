"""Проверка голосового тракта без игры и микрофона.

    python tools/selftest.py           # TTS -> STT по кругу
    python tools/selftest.py --play    # ещё и проиграть результат в динамики
    python tools/selftest.py --emotion anger
"""

import argparse
import logging
import sys
from pathlib import Path

import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion.config import ROOT, load_config  # noqa: E402
from companion.emotions import DEFAULT_EMOTION, EMOTIONS  # noqa: E402
from companion.voice.service import VoiceService  # noqa: E402

PHRASE = "Я поклялась защищать тебя и всё, что тебе принадлежит, мой тан."


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--play", action="store_true")
    ap.add_argument("--text", default=PHRASE)
    ap.add_argument("--emotion", default=DEFAULT_EMOTION, choices=list(EMOTIONS))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")

    cfg = load_config()
    cfg.push_to_talk.enabled = False
    service = VoiceService(cfg, use_llm=False)

    audio, sr = service.synthesize(args.text, args.emotion)
    out = ROOT / "selftest.wav"
    sf.write(out, audio, sr)
    print(f"TTS -> {out.name} ({len(audio) / sr:.1f} с, {sr} Гц, {args.emotion})")

    text = service.transcribe(audio, sr)
    print(f"Исходный текст:   {args.text}\nРаспознано (STT): {text}")

    if args.play:
        service.play(audio, sr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
