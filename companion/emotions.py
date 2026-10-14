"""Эмоции реплик. Коды совпадают с метками диалогов Скайрима (TRDT в Skyrim.esm),
по ним же разложены референсы голоса: characters/<имя>/voice/<эмоция>/*.wav.

puzzled (недоумение) в Скайриме есть, но у нас выключен: на референсах Эрика звучит плохо (2026-10-14).
Модели его не предлагаем; если всё же ответит puzzled — прозвучит как neutral."""

EMOTIONS: dict[str, str] = {
    "neutral": "спокойно",
    "happy": "радость",
    "anger": "злость",
    "fear": "страх",
    "sad": "грусть",
    "surprise": "удивление",
    "disgust": "отвращение",
}

DEFAULT_EMOTION = "neutral"


def parse_emotion(value: str | None) -> str | None:
    """Код, номер (с 1) или русское название -> код эмоции; None, если не распознано."""
    if not value:
        return None
    value = value.strip().lower()
    if value in EMOTIONS:
        return value
    if value.isdigit() and 1 <= int(value) <= len(EMOTIONS):
        return list(EMOTIONS)[int(value) - 1]
    for code, ru in EMOTIONS.items():
        if value == ru:
            return code
    return None


def emotions_hint() -> str:
    return "  ".join(f"{i}.{code}({ru})" for i, (code, ru) in enumerate(EMOTIONS.items(), 1))
