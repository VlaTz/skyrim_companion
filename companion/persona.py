"""Кто такой Гослинг — выжимкой для агентов вне разговора (исследователь, разговоры с NPC).

Полное описание персонажа (characters/<персонаж>/prompt.md) — для главного разговора: в нём кроме характера правила
разговора (поле do, инструменты, письма, мемы, память). Другим агентам эти правила вредны — начнут путать, что умеют.
Поэтому им — только разделы «ХАРАКТЕР» и «СТИЛЬ РЕЧИ»: чтобы и в вылазке, и с NPC Гослинг звучал собой.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

SECTIONS = ("ХАРАКТЕР", "СТИЛЬ РЕЧИ")
HEADING = re.compile(r"^[А-ЯЁA-Z][А-ЯЁA-Z ]+$")  # «ХАРАКТЕР», «СТИЛЬ РЕЧИ» — строка заглавными


def manner(prompt_file: Path, without: tuple[str, ...] = ()) -> str:
    """Характер и манера речи из описания персонажа. without — строки, где есть эти слова, убрать (с NPC — «Владос»:
    как обращаться к нему, там не нужно). "" — файла нет."""
    try:
        text = prompt_file.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("Персонаж: нет описания (%s) — агенты без его манеры", e)
        return ""
    keep, current, items = [], "", []
    for line in text.splitlines():
        if HEADING.match(line.strip()):
            current = line.strip()
            continue
        if current in SECTIONS and line.strip():
            items.append((current, line))
    for section in SECTIONS:
        lines = [line for name, line in items if name == section]
        # Пункт списка («- ...») с продолжением на следующих строках — целиком: убираем пункт, если в нём лишнее.
        points, point = [], []
        for line in lines:
            if line.lstrip().startswith("- ") and point:
                points.append(point)
                point = []
            point.append(line.strip())
        if point:
            points.append(point)
        points = [" ".join(p) for p in points if not any(w in " ".join(p) for w in without)]
        if points:
            keep.append(f"{section.capitalize()}:\n" + "\n".join(points))
    return "\n".join(keep)
