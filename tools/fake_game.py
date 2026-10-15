"""Имитация игры: шлёт в сервис события, как будет слать SKSE-плагин.

Сначала запусти сервис (start.bat), потом:

    python tools/fake_game.py              # меню сценариев
    python tools/fake_game.py whiterun     # один сценарий и выход
    python tools/fake_game.py --state      # что сервис сейчас знает об игре

Потом жми F8 и спрашивай Гослинга: «где мы?», «что тут происходит?».
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from companion.config import load_config  # noqa: E402

SCENARIOS: dict[str, tuple[str, list[dict]]] = {
    "whiterun": ("Вайтран, таверна вечером", [
        {"type": "game_loaded"},
        {"type": "status", "data": {
            "location": "Вайтран", "hold": "Вайтран", "cell": "Гарцующая кобыла",
            "interior": True, "hour": 20.5, "weather": "ясно", "health": 1.0, "in_combat": False,
            "level": 12, "nearby": [{"name": "Хульда"}, {"name": "Микаэль"}, {"name": "Утгерд Несломленная"}]}},
    ]),
    "road": ("Дорога из Вайтрана в Ривервуд, днём", [
        {"type": "status", "data": {
            "location": "", "hold": "Вайтран", "cell": "", "interior": False, "hour": 13.0, "weather": "облачно", "health": 1.0, "in_combat": False,
            "nearby": []}},
    ]),
    "barrow": ("Ветреный пик: бой с драуграми, игрок ранен", [
        {"type": "status", "data": {
            "location": "Ветреный пик", "hold": "Вайтран", "cell": "Ветреный пик",
            "interior": True, "hour": 2.0, "health": 0.8, "in_combat": False, "nearby": []}},
        {"type": "combat_started", "data": {"enemies": ["Драугр", "Драугр-воитель"]}},
        {"type": "status", "data": {
            "health": 0.25, "in_combat": True,
            "nearby": [{"name": "Драугр", "hostile": True}, {"name": "Драугр-воитель", "hostile": True}]}},
    ]),
    "victory": ("Конец боя: драугры убиты, найден трофей", [
        {"type": "actor_killed", "data": {"name": "Драугр-воитель", "by_player": True}},
        {"type": "combat_ended"},
        {"type": "status", "data": {
            "health": 0.4, "in_combat": False,
            "nearby": [{"name": "Драугр", "hostile": True, "dead": True},
                       {"name": "Драугр-воитель", "hostile": True, "dead": True}]}},
        {"type": "item_picked", "data": {"name": "Золотой коготь", "count": 1}},
    ]),
    "riften": ("Рифтен, рынок, утро", [
        {"type": "status", "data": {
            "location": "Рифтен", "hold": "Рифт", "cell": "Рифтен", "interior": False, "hour": 9.0,
            "weather": "туман", "health": 1.0, "in_combat": False,
            "nearby": [{"name": "Бриньольф"}, {"name": "Мадези"}, {"name": "Стражник Рифтена"}]}},
    ]),
    # Знакомый NPC стал врагом и убит, цель задания сменилась, выучено слово силы (статистика игры).
    # Первый quests — точка отсчёта, второй — уже событие «новая цель». Потом спроси: «что сейчас было?».
    "arvel": ("Ветреный пик: Арвел напал и убит, новая цель, слово силы", [
        {"type": "quests", "data": {"quests": [{"id": "0002610C", "name": "Ветреный пик", "type": 1,
                                                "objectives": ["Добыть золотой коготь"]}], "completed": []}},
        {"type": "status", "data": {
            "location": "Ветреный пик", "hold": "Фолкрит", "cell": "Ветреный пик - Храм", "interior": True,
            "health": 1.0, "in_combat": False, "nearby": [{"name": "Арвел Быстрый", "unique": True}]}},
        {"type": "status", "data": {"in_combat": True,
                                    "nearby": [{"name": "Арвел Быстрый", "hostile": True, "unique": True}]}},
        {"type": "combat_started", "data": {"enemies": ["Арвел Быстрый"]}},
        {"type": "combat_ended"},
        {"type": "actor_killed", "data": {"name": "Арвел Быстрый", "by_player": True, "unique": True}},
        {"type": "status", "data": {"in_combat": False, "nearby": [
            {"name": "Арвел Быстрый", "hostile": True, "dead": True, "unique": True}]}},
        {"type": "quests", "data": {"quests": [{"id": "0002610C", "name": "Ветреный пик", "type": 1,
                                                "objectives": ["Открыть дверь в Зале легенд"]}], "completed": []}},
        {"type": "stat", "data": {"name": "Words Of Power Learned", "value": 1}},
    ]),
    # Реплики NPC: сервис берёт текст из Skyrim.esm по номеру INFO. Встрянет ли Гослинг — решает модель
    # (через brain.overheard_quiet секунд тишины; обычно молчит, на шутку про колено — чаще отвечает).
    "overheard": ("Стражник рассказывает игроку про колено", [
        {"type": "dialogue_menu", "data": {"open": True}},
        {"type": "npc_line", "data": {"speaker": "Стражник Вайтрана", "file": "Skyrim.esm", "form": 0x20954,
                                      "category": 7, "subtype": 94, "to_player": True}},
        {"type": "dialogue_menu", "data": {"open": False}},
    ]),
    # Гибель и загрузка: Гослинг должен сказать прямо, что Владос погиб, — и посоветовать, что сделать иначе.
    "death": ("Западная башня: Мирмулнир убил Владоса, загрузка сейва в Вайтране", [
        {"type": "status", "data": {
            "location": "Западная сторожевая башня", "hold": "Вайтран", "cell": "", "interior": False,
            "health": 0.6, "in_combat": True, "nearby": [{"name": "Стражник Вайтрана", "hostile": False}],
            "threats": [{"name": "Мирмулнир", "race": "dragon race", "hostile": True, "distance": 60}]}},
        {"type": "combat_started", "data": {"enemies": ["Мирмулнир"], "races": {"Мирмулнир": "dragon race"}}},
        {"type": "actor_killed", "data": {"name": "Стражник Вайтрана", "by_player": False}},
        {"type": "status", "data": {"health": 0.12}},
        {"type": "player_died", "data": {"killer": "Мирмулнир", "race": "dragon race", "unique": True}},
        {"type": "game_loaded"},
        {"type": "status", "data": {
            "location": "Вайтран", "hold": "Вайтран", "cell": "", "interior": False, "health": 1.0,
            "in_combat": False, "nearby": []}},
    ]),
}


def request(base: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run(base: str, name: str) -> None:
    title, events = SCENARIOS[name]
    print(f"\n== {title}")
    for event in events:
        changes = request(base, "POST", "/game/event", event)["changes"]
        print(f"  -> {event['type']:15} {'; '.join(changes)}")
    print("\nСервис видит так (это уйдёт в промпт):\n")
    print(request(base, "GET", "/game/state")["context"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", nargs="?", choices=list(SCENARIOS))
    ap.add_argument("--state", action="store_true", help="показать состояние и выйти")
    args = ap.parse_args()

    cfg = load_config()
    base = f"http://{cfg.server.host}:{cfg.server.port}"
    try:
        if args.state:
            print(json.dumps(request(base, "GET", "/game/state"), ensure_ascii=False, indent=2))
        elif args.scenario:
            run(base, args.scenario)
        else:
            names = list(SCENARIOS)
            while True:
                print("\nСценарии:")
                for i, n in enumerate(names, 1):
                    print(f"  {i}. {SCENARIOS[n][0]}")
                choice = input("Номер (Enter — выход): ").strip()
                if not choice:
                    break
                if choice.isdigit() and 1 <= int(choice) <= len(names):
                    run(base, names[int(choice) - 1])
    except urllib.error.URLError as e:
        print(f"Сервис не отвечает на {base}: {e.reason}. Запусти start.bat.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
