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
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from companion.config import LLMConfig
from companion.emotions import DEFAULT_EMOTION, EMOTIONS

log = logging.getLogger(__name__)

RESPONSE_FORMAT = """
ФОРМАТ ОТВЕТА
Отвечай строго одним JSON-объектом, без пояснений и markdown:
{{"emotion": "<эмоция>", "text": "<что ты говоришь вслух>"}}
"emotion" — с какой интонацией ты это произносишь, одно из: {emotions}.
Выбирай по ситуации и смыслу реплики; по умолчанию — neutral.
"""


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
            if text:
                return Reply(text, emotion if emotion in EMOTIONS else DEFAULT_EMOTION)
        except json.JSONDecodeError:
            pass
    return Reply(content.strip().strip('"'), DEFAULT_EMOTION)


class CompanionLLM:
    def __init__(self, cfg: LLMConfig, prompt_file: Path):
        from openai import OpenAI

        if not prompt_file.is_file():
            raise FileNotFoundError(f"Нет промпта личности {prompt_file}")
        self.cfg = cfg
        self.prompt = prompt_file.read_text(encoding="utf-8").strip() + "\n" + RESPONSE_FORMAT.format(
            emotions=", ".join(EMOTIONS))
        # Локальным серверам ключ не нужен, но клиент требует непустую строку.
        api_key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else ""
        self.client = OpenAI(base_url=cfg.base_url, api_key=api_key or "not-needed",
                             timeout=cfg.timeout, max_retries=1)
        self.history: deque[dict] = deque(maxlen=cfg.history_turns * 2)
        log.info("LLM: %s @ %s", cfg.model, cfg.base_url)

    def system_prompt(self, memory: str = "", game_context: str = "") -> str:
        return (self.prompt
                .replace("{memory}", memory or "Пока ничего — вы только познакомились.")
                .replace("{game_context}", game_context or "Нет данных из игры."))

    def reply(self, player_text: str, game_context: str = "", memory: str = "") -> Reply:
        messages = [{"role": "system", "content": self.system_prompt(memory, game_context)},
                    *self.history,
                    {"role": "user", "content": player_text}]
        extra = {"response_format": {"type": "json_object"}} if self.cfg.json_mode else {}

        t = time.perf_counter()
        resp = self.client.chat.completions.create(
            model=self.cfg.model, messages=messages,
            temperature=self.cfg.temperature, max_tokens=self.cfg.max_tokens, **extra,
        )
        content = resp.choices[0].message.content or ""
        reply = parse_reply(content)
        log.info("LLM %.2f с: [%s] %r", time.perf_counter() - t, reply.emotion, reply.text)

        self.history.append({"role": "user", "content": player_text})
        self.history.append({"role": "assistant",
                             "content": json.dumps({"emotion": reply.emotion, "text": reply.text},
                                                   ensure_ascii=False)})
        return reply

    def reset(self) -> None:
        self.history.clear()
