"""Разговоры Гослинга с NPC: Гослинг говорит, NPC отвечает — вслух, своим голосом.

Обычный NPC на произвольную фразу не отвечает: у него только записанные в игре реплики. Поэтому ответ NPC пишет
модель в его роли — только по данным игры:
- кто он: справка о людях (companion/knowledge.py: профессия, где живёт);
- как он говорит и что знает: его собственные реплики из игры (datasets/game_quotes.tsv, tools/extract_quotes.py);
- что он говорил только что (подслушанное, GameState.heard).
Выдумывать имена, места, события, задания и слухи, которых нет в данных, ему запрещено: не знает — отмахивается.

Голос — его: реплика пишется в папку его типа голоса в моде (заглушки — под каждый тип голоса игры,
tools/build_gosling_esp.py), XTTS говорит по образцам его типа голоса из BSA (companion/game/voices.py), плагин
командует «скажи» от его имени (skse_plugin/src/Voice.cpp). Голоса нет — текстом на экране.

Память: слова NPC — «X сказал» (журнал, вид heard), а не факт о мире (MemoryKeeper.on_voice_event, npc_speech).
Разговор заводит исследователь (companion/explorer.py, поле talk): с кем и что сказать первым.
"""

from __future__ import annotations

import csv
import logging
import threading
from pathlib import Path
from typing import Callable

from companion import trace
from companion.config import TalkConfig
from companion.game.state import GameState, Nearby
from companion.game.voices import VoiceSamples

log = logging.getLogger(__name__)

NPC_SYSTEM = """Ты — {name}, житель Скайрима. К тебе обратился Гослинг — весёлый молодой норд, бывший картограф,
спутник путника Владоса. Ответь ему вслух, от себя, 1–2 короткими фразами, в своей манере — как в твоих репликах ниже.
Говори только то, что есть в данных о тебе ниже, и простое житейское (погода, работа, усталость, настроение).
Не выдумывай имён, мест, событий, заданий и слухов, которых нет в данных: не знаешь — так и скажи, отмахнись или
смени тему. Ты не обязан быть любезным: занят, не любишь чужаков — так и ответь.
Отвечай строго одним JSON-объектом: {{"say": "<что ты говоришь>", "end": <true — ты закончил разговор>}}"""
GOSLING_SYSTEM = """Ты — Гослинг, молодой норд, бывший картограф из Солитьюда, спутник Владоса. Ты исследуешь и
разговариваешь с {name}; зачем: {goal}. Продолжи разговор одной короткой фразой — спроси то, что хочешь узнать,
отзовись на его слова, — или поблагодари и закончи. Говоришь с ним, а не с Владосом. Не выдумывай фактов о мире.
Ты — это ты: держись того, что решил о себе, и своего настроения; задели его слова лично — пусть это слышно.
{manner}
Отвечай строго одним JSON-объектом: {{"say": "<что ты говоришь; пусто — молчишь>", "end": <true — заканчиваешь>}}"""


class Talk:
    def __init__(self, cfg: TalkConfig, game: GameState, complete: Callable[..., dict | None],
                 deliver: Callable[[str], bool], say_as: Callable[[int, str, str, list, str], bool],
                 samples: VoiceSamples, quotes_path: Path, people: Callable[[str], str] = lambda name: "",
                 model: str = "", me: Callable[[], str] = lambda: "", manner: str = "",
                 feel: Callable[[str, str], str] = lambda name, text: ""):
        self.cfg = cfg
        self.game = game
        self.complete = lambda system, user, **kw: complete(system, user, model=cfg.model or model or None, **kw)
        self.deliver = deliver  # реплика Гослинга (VoiceService.deliver)
        self.say_as = say_as    # реплика NPC его голосом (VoiceService.say_as)
        self.samples = samples
        self.quotes_path = quotes_path
        self.people = people    # справка о человеке по имени (база знаний), "" — нет
        self.me = me            # он сам: душа и настроение — чтобы и с NPC оставался собой
        self.manner = manner    # характер и манера речи (companion/persona.py)
        self.feel = feel        # слова NPC — в душу и настроение: задело лично — что именно ("" — нет)
        self._quotes: dict[str, list[str]] | None = None
        self._lock = threading.Lock()

    def distance(self, npc: Nearby) -> float | None:
        """От Гослинга, а не от Владоса (24.10: Гослинг стоял у Изольды, Владос — поодаль: «далеко»)."""
        return npc.gosling_distance if npc.gosling_distance is not None else npc.distance

    def too_far(self, npc: Nearby) -> bool:
        distance = self.distance(npc)
        return distance is None or distance > self.cfg.distance

    def unavailable(self, npc: Nearby, near: bool = False) -> str:
        """Почему с ним сейчас не заговорить; "" — можно. near — игра сама сказала, что Гослинг подошёл к нему (поход
        «к человеку» выполнен): расстояние из статуса тогда не проверять — оно приходит с опозданием (24.10: подошёл на
        2,6 м, а по статусу «далеко» — и ещё три подхода впустую)."""
        if not self.cfg.enabled:
            return "разговоры выключены"
        if npc.hostile or npc.dead:
            return "враг или мёртв"
        if not self.game.hears():
            return "Владоса нет рядом — мир вокруг не просчитан, людям не до тебя"
        if not near and self.too_far(npc):
            return f"он далеко (дальше {self.cfg.distance:.0f} м) — сначала подойди"
        return ""

    def converse(self, npc: Nearby, opener: str, goal: str) -> list[str]:
        """Разговор: первая фраза Гослинга opener, дальше по очереди (до talk.turns ответов NPC). -> строки разговора."""
        lines: list[str] = []
        if not opener or not self.deliver(opener):
            return lines
        lines.append(f"Гослинг: «{opener}»")
        voice = self.samples.edid(npc.voice)
        refs = self.samples.refs(voice) if voice else []
        about = self._about(npc)
        trace.event(f"Разговор с {npc.title}: голос {voice or 'не найден'}, образцов {len(refs)}")
        for turn in range(max(1, self.cfg.turns)):
            reply = self.complete(NPC_SYSTEM.format(name=npc.name),
                                  about + "\nРазговор:\n" + "\n".join(lines) + f"\nТвой ответ ({npc.name}):",
                                  title=f"Разговор: {npc.name} отвечает")
            text = str(reply.get("say") or "").strip() if isinstance(reply, dict) else ""
            if not text:
                break
            self.say_as(npc.form, npc.name, voice, refs, text)
            lines.append(f"{npc.name}: «{text}»")
            touched = self.feel(npc.name, text)  # и настроение сдвигается, как от любого события
            if reply.get("end") or turn == self.cfg.turns - 1:
                break
            me = self.me()
            mine = self.complete(GOSLING_SYSTEM.format(name=npc.name, goal=goal or "узнать о месте и о нём",
                                                       manner=self.manner),
                                 (f"Ты сам:\n{me}\n\n" if me else "")
                                 + (f"Его слова задели тебя лично: {touched}\n\n" if touched else "")
                                 + "Разговор:\n" + "\n".join(lines) + "\nТвоя реплика (Гослинг):",
                                 title=f"Разговор: Гослинг — {npc.name}")
            text = str(mine.get("say") or "").strip() if isinstance(mine, dict) else ""
            if not text or not self.deliver(text):
                break
            lines.append(f"Гослинг: «{text}»")
            if mine.get("end"):
                break
        log.info("Разговор с %s: %d реплик", npc.name, len(lines))
        return lines

    def _about(self, npc: Nearby) -> str:
        """Данные о нём для роли: кто он, где сейчас, как говорит в игре, что говорил только что."""
        parts = [f"Кто ты (из данных игры): {self.people(npc.name) or npc.title}"]
        if here := self.game.status.location:
            parts.append(f"Где ты сейчас: {here}")
        if quotes := self._quotes_of(npc.name):
            parts.append("Так ты говоришь в игре (твои реплики — по ним твоя манера и что ты знаешь):\n"
                         + "\n".join(f"- «{q}»" for q in quotes))
        if recent := [f"«{h.text}»" for h in list(self.game.heard) if h.speaker == npc.name][-3:]:
            parts.append(f"Что ты говорил только что: {' / '.join(recent)}")
        return "\n".join(parts)

    def _quotes_of(self, name: str) -> list[str]:
        with self._lock:
            if self._quotes is None:
                self._quotes = {}
                try:
                    with open(self.quotes_path, encoding="utf-8") as f:
                        for row in csv.DictReader(f, delimiter="\t"):
                            if (who := row.get("speaker") or "") and (text := row.get("text") or ""):
                                self._quotes.setdefault(who, []).append(text)
                except OSError as e:
                    log.warning("Разговоры: нет реплик игры (%s) — роли NPC без них", e)
        found = self._quotes.get(name, [])
        # Длинные — содержательнее («Огненную соль можно добыть…»), чем «Да?» и «Хм».
        unique = list(dict.fromkeys(sorted(found, key=len, reverse=True)))
        return unique[:self.cfg.quotes]
