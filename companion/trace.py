"""Трасса Гослинга: всё, что он делал и почему, — в отдельный файл logs/trace/<дата>.log (не в консоль).

На каждый ход — один блок: что запустило (фраза игрока, событие, подслушанное, тишина), какие обстановка и
память ушли модели, какие справки она вызвала и что получила, что ответила (эмоция, текст, remember, интерес),
сколько это заняло. Отдельные строки — чем кончилась озвучка (из Гослинга, в колонки, перебили) и блоки
летописца (что ушло в запрос дневника и что вернулось). Блок пишется разом — ходы из разных потоков не
перемешиваются.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("companion.trace")
log.propagate = False  # только в свой файл
log.addHandler(logging.NullHandler())


def setup(directory: Path) -> Path:
    """Включить трассу: файл на день запуска. Возвращает путь к файлу."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now():%Y-%m-%d}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.info(f"\n######## Запуск сервиса {datetime.now():%Y-%m-%d %H:%M:%S} ########")
    return path


def event(text: str) -> None:
    """Одна строка: «11:52:38 Озвучка: …»."""
    log.info(f"{datetime.now():%H:%M:%S} {text}")


class Block:
    """Блок хода: собираем строки, пишем разом в write()."""

    def __init__(self, title: str):
        self.started = time.perf_counter()
        self.lines = [f"\n════ {datetime.now():%H:%M:%S} {title}"]

    def section(self, name: str, text: str) -> None:
        self.lines.append(f"[{name}]")
        self.lines.append(text.strip() or "—")

    def line(self, text: str) -> None:
        self.lines.append(text)

    def write(self) -> None:
        self.lines.append(f"──── {time.perf_counter() - self.started:.2f} с")
        log.info("\n".join(self.lines))
