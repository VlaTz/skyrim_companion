"""Переписка с Гослингом, когда он далеко: дальше слышимости, но он твой спутник (послал, ждёт, отстал).

Голосом вдали не слышно — поэтому письма:
- Владос нажимает F8: речь распознаётся и уходит Гослингу письмом (companion/voice/service.py); его ответ — текстом
  на экране игры (уведомления HUD, команда notify плагину), без голоса.
- Гослинг пишет и сам — мозг (companion/brain.py): по важным своим событиям (дошёл, напали, отбился, увидел новое
  место, ранен, приказ не вышел) и когда долго ждёт.
- Приказы в письме — как голосом (поле "do"): «иди ко мне» — follow, и игра сама приводит спутника.
Отвечает Гослинг из своей обстановки, а не Владоса: где он сам и что с ним (MemoryKeeper.letter_context).
"""

from __future__ import annotations

import logging
from typing import Callable

from companion import trace
from companion.game.state import GameState
from companion.voice.xtts import split_text

log = logging.getLogger(__name__)

LINE = 90  # знаков в одном уведомлении: длинную строку игра может обрезать — режем по предложениям (проверить в игре)
FROM_PLAYER = "Ты пишешь Гослингу"
FROM_GOSLING = "Гослинг пишет"


class Letters:
    def __init__(self, send_command: Callable[[dict], None], plugin_connected: Callable[[], bool], game: GameState,
                 context: Callable[[], str]):
        self.send_command = send_command
        self.plugin_connected = plugin_connected
        self.game = game
        self.context = context  # «Сейчас в игре» для письма — обстановка Гослинга

    def available(self) -> bool:
        """Можно переписываться: игра на связи, Гослинг жив и твой спутник, но не слышит тебя (далеко)."""
        g = self.game.status.gosling
        return bool(self.plugin_connected() and g and not g.dead and (g.follower or g.waiting) and not self.game.hears())

    def show(self, text: str, author: str) -> None:
        """Текст на экран игры — уведомлениями, по строке не длиннее LINE."""
        pieces = split_text(f"{author}: {text}", LINE)
        trace.event(f"Письмо на экран ({author}): «{text}»")
        log.info("Письмо (%s, %d строк): %s", author, len(pieces), text)
        for piece in pieces:
            self.send_command({"type": "notify", "text": piece})
