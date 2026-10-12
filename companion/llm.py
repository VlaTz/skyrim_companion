"""Ответы компаньона: любая LLM с OpenAI-совместимым API (LM Studio, Ollama, OpenRouter ...).

Модель получает промпт личности из characters/<персонаж>/prompt.md и отвечает
JSON-ом {"emotion": ..., "text": ...}: текст озвучивается, эмоция выбирает
референсы голоса.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from companion.config import LLMConfig
from companion.emotions import DEFAULT_EMOTION, EMOTIONS
from companion.tools import Tool

log = logging.getLogger(__name__)

RESPONSE_FORMAT = """
ФОРМАТ ОТВЕТА
Отвечай строго одним JSON-объектом, без пояснений и markdown:
{{"emotion": "<эмоция>", "text": "<что ты говоришь вслух>"}}
"emotion" — с какой интонацией ты это произносишь, одно из: {emotions}.
Выбирай по ситуации и смыслу реплики; по умолчанию — neutral.
"""

# Пока нет команд в игру (кусок 6 плана) — честно говорим модели, что она умеет только говорить.
CAPABILITIES = """
ТВОИ ВОЗМОЖНОСТИ
Пока ты можешь только говорить. По просьбе голосом ты не умеешь идти, ждать, следовать,
осматриваться, сражаться или брать вещи — это игрок выбирает в обычном разговоре с тобой.
Если просят что-то сделать, не обещай и не говори, что уже делаешь: пошути или скажи,
что пока останешься рядом.
"""

SELF_TALK = """
КАК УСТРОЕНЫ СООБЩЕНИЯ
В начале последнего сообщения — блок «Сейчас в игре»: самая свежая обстановка. Если она расходится
с тем, что ты говорил раньше, верь ей — вы могли уже уйти, войти куда-то, закончить бой.
«Игрок: …» — слова игрока. На них отвечай всегда, пустой text нельзя.
Сообщения в квадратных скобках — не слова игрока, а то, что сейчас происходит. На них ты можешь
сказать что-то от себя (коротко, к месту, не повторяя уже сказанное) или промолчать — тогда
верни пустой text. Молчать — нормально: лучше промолчать, чем болтать без повода.
"""
TOOLS_NOTE = """
СПРАВКИ
У тебя есть инструменты-справки об игре. Вызывай их, только если для ответа нужно то, чего нет в
«Сейчас в игре», — каждая справка задерживает ответ. Получив справку, отвечай как обычно, JSON-ом.
Не пересказывай справку целиком — скажи главное, как сказал бы спутнику вслух.
"""
GAME_CONTEXT_NOTE ="Описана в начале последнего сообщения, в блоке «Сейчас в игре»."


@dataclass
class Reply:
    text: str
    emotion: str


def parse_reply(content: str) -> Reply:
    """Достаёт {"emotion", "text"} из ответа модели; не-JSON считается репликой с neutral."""
    match = re.search(r"\{.*\}", content, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            text = str(data.get("text", "")).strip()
            emotion = str(data.get("emotion", "")).strip().lower()
            if text or "text" in data:  # пустой text — модель решила промолчать
                return Reply(text, emotion if emotion in EMOTIONS else DEFAULT_EMOTION)
        except json.JSONDecodeError:
            pass
    log.warning("LLM ответила не чистым JSON: %r", content)
    # JSON оборван (например, по max_tokens) — берём текст, который успел прийти, а не сырой JSON.
    if text_match := re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)', content):
        try:
            text = json.loads(f'"{text_match.group(1)}"').strip()
        except json.JSONDecodeError:
            text = text_match.group(1).strip()
        # Не озвучивать оборванное слово: обрезаем до последнего целого предложения, если оно есть.
        if (end := max(text.rfind(c) for c in ".!?…")) > 0:
            text = text[:end + 1]
        emotion_match = re.search(r'"emotion"\s*:\s*"(\w+)"', content)
        emotion = emotion_match.group(1).lower() if emotion_match else ""
        if text:
            return Reply(text, emotion if emotion in EMOTIONS else DEFAULT_EMOTION)
    return Reply(content.strip().strip('"'), DEFAULT_EMOTION)


class CompanionLLM:
    def __init__(self, cfg: LLMConfig, prompt_file: Path):
        from openai import OpenAI

        if not prompt_file.is_file():
            raise FileNotFoundError(f"Нет промпта личности {prompt_file}")
        self.cfg = cfg
        self.prompt = (prompt_file.read_text(encoding="utf-8").strip() + "\n" + CAPABILITIES + SELF_TALK
                       + RESPONSE_FORMAT.format(emotions=", ".join(EMOTIONS)))
        # Локальным серверам ключ не нужен, но клиент требует непустую строку.
        api_key = cfg.api_key or (os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else "")
        local = any(h in cfg.base_url for h in ("localhost", "127.0.0.1", "[::1]"))
        if not api_key and not local:
            raise RuntimeError(
                f"Нет ключа для {cfg.base_url}: укажи llm.api_key в config.local.yaml "
                f"или переменную окружения {cfg.api_key_env}")
        self.client = OpenAI(base_url=cfg.base_url, api_key=api_key or "not-needed",
                             timeout=cfg.timeout, max_retries=1)
        self.history: deque[dict] = deque(maxlen=cfg.history_turns * 2)
        self._lock = threading.Lock()  # отвечают и на реплики игрока, и на события — по очереди
        self.tools: dict[str, Tool] = {}
        log.info("LLM: %s @ %s", cfg.model, cfg.base_url)

    def system_prompt(self, memory: str = "", with_tools: bool = False) -> str:
        # Обстановка — не здесь, а в последнем сообщении: так модель не тянется за старыми ответами.
        prompt = (self.prompt
                  .replace("{memory}", memory or "Пока ничего — вы только познакомились.")
                  .replace("{game_context}", GAME_CONTEXT_NOTE))
        return prompt + TOOLS_NOTE if with_tools else prompt

    def set_tools(self, tools: list[Tool]) -> None:
        self.tools = {t.name: t for t in tools}
        log.info("Инструменты агента: %s", ", ".join(self.tools) or "нет")

    def reply(self, player_text: str, game_context: str = "", memory: str = "") -> Reply:
        """Ответ на реплику игрока. Молчать в ответ нельзя — пустой ответ переспрашиваем один раз."""
        reply = self._ask(f"Игрок: {player_text}", game_context, memory, use_tools=True)
        if not reply.text:
            log.warning("LLM промолчала в ответ игроку — переспрашиваем")
            reply = self._ask(f"Игрок: {player_text}", game_context, memory, use_tools=True)
        return reply

    def initiate(self, situation: str, game_context: str = "", memory: str = "") -> Reply:
        """Реплика по своей инициативе на событие или тишину; пустой text — решил промолчать.
        Без инструментов: тут важнее быстро."""
        return self._ask(f"[{situation}]", game_context, memory, use_tools=False)

    def _ask(self, user_content: str, game_context: str, memory: str, use_tools: bool) -> Reply:
        """user_content уходит в историю как есть; обстановка «Сейчас в игре» и обмен
        с инструментами — только в этот запрос."""
        now = f"Сейчас в игре:\n{game_context or 'Нет данных из игры.'}\n\n{user_content}"
        tools = list(self.tools.values()) if use_tools and self.cfg.tools else []
        with self._lock:
            messages = [{"role": "system", "content": self.system_prompt(memory, with_tools=bool(tools))},
                        *self.history,
                        {"role": "user", "content": now}]

            t = time.perf_counter()
            calls = 0
            # Цикл агента: модель может запросить справки (до max_tool_rounds раз), потом отвечает.
            for round_ in range(self.cfg.max_tool_rounds + 1):
                msg = self._complete(messages, tools, final=round_ == self.cfg.max_tool_rounds)
                if not msg.tool_calls:
                    break
                messages.append({"role": "assistant", "content": msg.content or "",
                                 "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
                for tc in msg.tool_calls:
                    calls += 1
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": self._run_tool(tc)})
            reply = parse_reply(msg.content or "")
            log.info("LLM %.2f с%s: [%s] %r", time.perf_counter() - t,
                     f" (инструментов: {calls})" if calls else "", reply.emotion, reply.text)

            if reply.text:  # молчание на событие в историю не пишем
                self.history.append({"role": "user", "content": user_content})
                self.history.append({"role": "assistant",
                                     "content": json.dumps({"emotion": reply.emotion, "text": reply.text},
                                                           ensure_ascii=False)})
            return reply

    def _complete(self, messages: list[dict], tools: list[Tool], final: bool):
        kwargs: dict = {}
        if self.cfg.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if tools:
            kwargs["tools"] = [t.schema() for t in tools]
            if final:  # справок хватит — пора отвечать (сами tools оставляем: в истории есть их вызовы)
                kwargs["tool_choice"] = "none"
        resp = self.client.chat.completions.create(
            model=self.cfg.model, messages=messages,
            temperature=self.cfg.temperature, max_tokens=self.cfg.max_tokens,
            extra_body=self.cfg.extra_body or None, **kwargs,
        )
        if resp.choices[0].finish_reason == "length":
            log.warning("Ответ LLM обрезан по max_tokens (%d)", self.cfg.max_tokens)
        return resp.choices[0].message

    def _run_tool(self, call) -> str:
        name = call.function.name
        t = time.perf_counter()
        try:
            args = json.loads(call.function.arguments or "{}")
            tool = self.tools.get(name)
            result = tool.handler(**args) if tool else f"Нет такого инструмента: {name}"
        except Exception as e:  # noqa: BLE001 — ошибка инструмента не должна ронять ответ
            log.exception("Инструмент %s упал", name)
            args, result = call.function.arguments, f"Ошибка инструмента: {e}"
        log.info("Инструмент %s(%s) %.0f мс -> %r", name, args, (time.perf_counter() - t) * 1000, result[:200])
        return result[:3000]

    def reset(self) -> None:
        self.history.clear()
