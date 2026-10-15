"""Свои шутки Гослинга по темам — сильной моделью, в characters/gosling/jokes.yaml с пометкой new: true.

Тема — набор меток (город, место, событие) и опорные факты лора, чтобы модель шутила о настоящем, а не
выдумывала (companion/jokes.py: TOPICS, JokeWriter — тот же писатель дописывает шутки и в игре, в фоне).
Модель видит уже имеющиеся шутки с этими метками (не повторять). Новые дописываются в конец файла строками,
комментарии и порядок файла не трогаются; игрок просматривает их и удаляет неудачные.

    python tools/generate_jokes.py --list                      # темы
    python tools/generate_jokes.py Рифтен Маркарт              # только эти темы
    python tools/generate_jokes.py --all                        # все
    python tools/generate_jokes.py --all --per-group 8 --model anthropic/claude-opus-5.5

Модель по умолчанию — Sonnet 5.5: в сравнении 8 моделей на одних темах — почти как Opus, вдвое дешевле
(production_logs/2026-10-15_шутки.md). Opus чуть смешнее: --model anthropic/claude-opus-5.5.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from companion.config import load_config  # noqa: E402
from companion.jokes import TOPICS, WRITER_MODEL, JokeBook, JokeWriter  # noqa: E402
from companion.llm import CompanionLLM  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("groups", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--per-group", type=int, default=6)
    ap.add_argument("--batch", type=int, default=4, help="тем в одном запросе")
    ap.add_argument("--model", default=WRITER_MODEL)
    args = ap.parse_args()
    if args.list:
        for name, tags, _facts in TOPICS:
            print(f"{name}: {', '.join(tags)}")
        return 0
    chosen = [t for t in TOPICS if args.all or t[0] in args.groups]
    if not chosen:
        ap.error("укажи темы (--list) или --all")

    cfg = load_config()
    path = cfg.character_path / "jokes.yaml"
    book = JokeBook(path, cfg.character_path / "jokes_auto.yaml")  # дубли не пишем и против дописанного в игре
    llm = CompanionLLM(cfg.llm, cfg.character_path / "prompt.md")
    added = JokeWriter(book, llm.complete_json, args.model).write(chosen, path, args.per_group, args.batch, print)
    print(f"Готово: {added} новых -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
