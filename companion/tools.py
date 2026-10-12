"""Инструменты агента: справки, которые модель запрашивает сама, когда для ответа не хватает
обстановки из «Сейчас в игре». Тяжёлое и редкое (места на карте, позже — квесты, память, книги)
держим здесь, а не в каждом промпте.

Данные — из кэша сервиса (плагин присылает заранее), поэтому инструмент отвечает мгновенно.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from companion.game.state import GameState


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema аргументов
    handler: Callable[..., str]

    def schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


def build_tools(game: GameState) -> list[Tool]:
    return [
        Tool(
            name="find_places",
            description=(
                "Найти места на карте игрока: по типу, части названия и/или расстоянию. Отвечает расстоянием "
                "и стороной света от игрока. Знает только открытые игроком места; неоткрытые — только "
                "совсем рядом и без названия. Вызывай, если спрашивают, где что-то находится, куда пойти, "
                "что есть поблизости — и этого нет в «Сейчас в игре»."),
            parameters={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": (
                        "Тип места, часть слова: город, поселение, пещера, форт, руины, шахта, ферма, лагерь, "
                        "логово дракона, святилище, камень-хранитель, маяк, перевал, хижина. Пусто — любые.")},
                    "name": {"type": "string", "description": "Часть названия места. Пусто — любое."},
                    "max_km": {"type": "number", "description": "Не дальше стольких км. 0 — без ограничения."},
                },
            },
            handler=game.find_places,
        ),
        Tool(
            name="quests",
            description=(
                "Активные квесты из журнала игрока: название, тип и текущие цели. Вызывай, если спрашивают, "
                "что нам делать, куда идти, какие есть задания."),
            parameters={"type": "object", "properties": {}},
            handler=game.quest_log.summary,
        ),
        Tool(
            name="quest_details",
            description=(
                "Подробности одного активного квеста: текущие цели и последняя запись в журнале (что "
                "произошло и что делать дальше). Вызывай, когда нужен конкретный квест."),
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Название квеста или его часть."}},
                "required": ["name"],
            },
            handler=game.quest_log.details,
        ),
        Tool(
            name="player_details",
            description=(
                "Подробности об игроке: золото, здоровье/магия/запас сил, что надето и в руках, что в сумке "
                "(по категориям, самое ценное), активные эффекты и болезни. Вызывай, если спрашивают про "
                "снаряжение, деньги, зелья, припасы, хватит ли на покупку, чем лечиться."),
            parameters={"type": "object", "properties": {}},
            handler=game.player_details,
        ),
    ]
