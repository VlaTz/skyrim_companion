"""Имя Игрока — из config.yaml (секция player), а не в коде.

В коде, запросах и текстах событий Игрок — «Владос»: внутреннее имя-метка со склонением (Владоса, Владосу, Владосом,
Владосе). Модели и в трассу уходит имя из конфига — подставляется на границе: в каждом запросе к модели (клиент
CompanionLLM — разговор, фоновые запросы, зрение) и в строках трассы. Так не переписывать 400+ строк запросов (шаблоны
с JSON, склонение), а старая память с «Владос» сама говорит новым именем.

Падежи обычного мужского имени на согласную выводятся сами (Иван → Ивана, Ивану, Иваном, Иване); иначе — player.forms.
"""

from __future__ import annotations

import re

CANON = "Владос"
CASES = {"name": CANON, "gen": "Владоса", "dat": "Владосу", "ins": "Владосом", "pre": "Владосе"}
# Имя-метка во всех падежах; перед ним — «о» (о Владосе -> об Иване).
_CANON_RE = re.compile(r"(?:\b([Оо]) )?\b(" + "|".join(sorted(CASES.values(), key=len, reverse=True)) + r")\b")
_to: dict[str, str] | None = None  # форма-метка -> форма имени из конфига; None — имя то же (ничего не меняем)


def decline(name: str) -> dict[str, str]:
    """Падежи имени по окончанию: на согласную — +а/у/ом/е; на -й, -ь — я/ю/ем/е; на -а, -я — ы(и)/е/ой(ей)/е."""
    stem, last = name[:-1], name[-1:].lower()
    if last in "йь":
        return {"name": name, "gen": stem + "я", "dat": stem + "ю", "ins": stem + "ем", "pre": stem + "е"}
    if last in "ая":
        soft = last == "я" or stem[-1:].lower() in "гкхжшщч"
        return {"name": name, "gen": stem + ("и" if soft else "ы"), "dat": stem + "е",
                "ins": stem + ("ей" if last == "я" else "ой"), "pre": stem + "е"}
    if not re.search(r"[а-яё]$", name, re.I):  # не по-русски (Vlados) — не склоняем
        return {case: name for case in CASES}
    return {"name": name, "gen": name + "а", "dat": name + "у", "ins": name + "ом", "pre": name + "е"}


def setup(name: str, forms: dict[str, str] | None = None) -> None:
    """Имя Игрока из конфига; forms — свои падежи (gen, dat, ins, pre) поверх выведенных."""
    global _to
    name = (name or "").strip()
    if not name or (name == CANON and not forms):
        _to = None
        return
    mine = decline(name) | {k: v for k, v in (forms or {}).items() if k in CASES and v}
    _to = {CASES[case]: mine[case] for case in CASES}


def name() -> str:
    """Как зовут Игрока (имя из конфига)."""
    return _to[CANON] if _to else CANON


def apply(text: str) -> str:
    """Имя-метка во всех падежах -> имя из конфига."""
    if not _to or not text:
        return text
    def name_for(m: re.Match) -> str:
        form, before = _to[m.group(2)], m.group(1)
        if before and form[:1].lower() in "аеёиоуыэюя":
            before += "б"
        return f"{before} {form}" if before else form
    return _CANON_RE.sub(name_for, text)


def pattern() -> str:
    """Регулярное выражение на имя Игрока во всех падежах (для поиска в ответах модели)."""
    forms = _to.values() if _to else CASES.values()
    return "|".join(re.escape(f) for f in sorted(set(forms), key=len, reverse=True))


def _deep(value):
    if isinstance(value, str):
        return apply(value)
    if isinstance(value, list):
        return [_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: v if k == "url" else _deep(v) for k, v in value.items()}  # url — кадр зрения (base64), не текст
    return value


def request(kwargs: dict) -> dict:
    """Аргументы запроса к модели: имя — в сообщениях и описаниях инструментов (кадр зрения не трогаем)."""
    if not _to:
        return kwargs
    out = dict(kwargs)
    for key in ("messages", "tools"):
        if key in out and out[key] is not None:
            out[key] = _deep(out[key])
    return out
