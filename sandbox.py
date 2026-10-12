"""Песочница: тестовый запрос к LLM с промптом Гослинга.

Адрес, модель и ключ — из config.yaml / config.local.yaml (секция llm).

    python sandbox.py                                   # модель из конфига
    python sandbox.py google/gemini-3.1-flash-lite      # другая модель для сравнения
"""

import logging
import sys
import time

from companion.config import load_config
from companion.llm import CompanionLLM

PHRASES = [
    ("Кто ты такой?", "Таверна «Гарцующая кобыла», Вайтран, вечер."),
    ("Сзади! Драугр!", "Бой в нордских руинах, игрок ранен."),
    ("Что такое Ту'ум?", "Дорога из Вайтрана в Ривервуд, спокойно."),
    ("Меня опять обокрали в Рифтене.", "Рифтен, рынок."),
]


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    cfg = load_config()
    if len(sys.argv) > 1:
        cfg.llm.model = sys.argv[1]

    llm = CompanionLLM(cfg.llm, cfg.character_path / "prompt.md")
    print(f"Модель: {cfg.llm.model}\n")
    for text, context in PHRASES:
        t = time.perf_counter()
        reply = llm.reply(text, game_context=context)
        print(f"Игрок:   {text}   ({context})")
        print(f"Гослинг: [{reply.emotion}] {reply.text}   [{time.perf_counter() - t:.2f} с]\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
