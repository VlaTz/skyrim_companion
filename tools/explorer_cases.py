"""Кейсы исследователя и разговоров с NPC — на настоящей модели, без игры.

Данные — настоящие: места и люди из файлов игры (справка о местах, база знаний, реплики игры), а то, что Гослинг знает
о себе (душа), о месте (факты) и его настроение, — из последнего трейса (logs/trace). Игра подменена заглушкой:
кто рядом, что слышно — задано в кейсе. Видно, что модель решает, что говорит и звучит ли Гослинг собой.

    python tools/explorer_cases.py              # все кейсы
    python tools/explorer_cases.py --case talk  # один: start | hint | talk | mood
    python tools/explorer_cases.py --prompts    # ещё и запросы целиком
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion.config import ROOT, load_config  # noqa: E402
from companion.explorer import Explorer  # noqa: E402
from companion.game.places import PlaceBook  # noqa: E402
from companion.game.state import Heard, Nearby  # noqa: E402
from companion.knowledge import Knowledge  # noqa: E402
from companion.llm import CompanionLLM  # noqa: E402
from companion.persona import manner  # noqa: E402
from companion.talk import Talk  # noqa: E402


def from_trace(trace_dir: Path) -> tuple[str, list[str], str]:
    """(душа, факты о месте, настроение) — последние из трейса."""
    files = sorted(trace_dir.glob("*.log"))
    text = files[-1].read_text(encoding="utf-8") if files else ""
    soul = re.findall(r"(Ты сам \(что ты о себе решил.*?)\n(?:Цели|Договорённости|Где вы)", text, re.S)
    facts = re.findall(r"Что ты сам узнал об этом месте и людях рядом:\n((?:- .*\n)+)", text)
    mood = re.findall(r"(Твоё настроение сейчас \(из того, что было\): [^;\n]*)", text)
    return (soul[-1] if soul else "", [f for f in facts[-1].splitlines() if f] if facts else [],
            mood[-1] if mood else "")


class Game:
    """Заглушка игры: Гослинг и Владос в Вайтране, вокруг — люди кейса."""

    def __init__(self, people: list[Nearby], heard: list[tuple[str, str, bool]]):
        g = NS(pos=None, location="Вайтран", cell="", interior=False, loaded=True, distance=3, bleedout=False,
               health=1.0, follower=True, bag=[], items=[], doors=[], nearby=None)
        self.status = NS(gosling=g, pos=None, location="Вайтран", nearby=people)
        self.markers, self.task, self.task_done, self.outing = [], None, False, ""
        self.heard = deque(Heard(time.time(), who, text, to_player) for who, text, to_player in heard)

    def hears(self) -> bool:
        return True

    def events_since(self, _t: float) -> list[str]:
        return []

    def set_outing(self, _who: str, text: str, **_kw) -> None:
        self.outing = text


class Orders:
    def __init__(self):
        self.sent = []

    def _go(self, what: str) -> str:
        self.sent.append(what)
        return f"g{len(self.sent)}"

    def go_ref(self, _form, name, _lead, arrive=0):
        return self._go(name)

    def go_spot(self, _spot, name, _lead):
        return self._go(name)

    def go_marker(self, marker, _lead):
        return self._go(marker.name)

    def command(self, action):
        return self._go(action)


PEOPLE = [Nearby(name="Хеймскр", form=1, distance=12, gosling_distance=12, unique=True),
          Nearby(name="Саадия", form=2, distance=25, gosling_distance=25, unique=True, race="Редгард"),
          Nearby(name="Стражник Вайтрана", form=3, distance=6, gosling_distance=6),
          Nearby(name="Назим", form=4, distance=9, gosling_distance=9, unique=True),
          Nearby(name="Белетор", form=5, distance=4, gosling_distance=4, unique=True)]
HEARD = [("Хеймскр", "Талос — бог людей! Талмор боится правды!", False),
         ("Назим", "Вы бывали в Облачном районе? О, о чём я, конечно же нет.", True)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", choices=["start", "hint", "talk", "mood"], help="один кейс")
    ap.add_argument("--prompts", action="store_true", help="показать запросы целиком")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    cfg = load_config()
    llm = CompanionLLM(cfg.llm, cfg.character_path / "prompt.md")
    places = PlaceBook(cfg.game.data_dir)
    knowledge = Knowledge(cfg.game.data_dir, places)
    soul, facts, mood_line = from_trace(cfg.logs_path / "trace")
    prompt_file = cfg.character_path / "prompt.md"
    print(f"Из трейса: душа {len(soul)} симв., фактов о месте {len(facts)}, настроение: {mood_line[:80] or 'нет'}\n")
    calls = []

    def complete(system, user, **kw):
        t = time.perf_counter()
        result = llm.complete_json(system, user, **kw)
        calls.append((kw.get("title", ""), len(system) + len(user), time.perf_counter() - t))
        if args.prompts:
            print(f"--- запрос «{kw.get('title')}»:\n{system}\n\n{user}\n")
        return result

    def explorer(game: Game, mood: str = "") -> tuple[Explorer, Orders]:
        orders = Orders()
        ex = Explorer(cfg.explorer, game, orders, complete, places, lambda: soul, deliver=lambda s: True,
                      record=lambda c: None, mood=lambda: mood or mood_line, manner=manner(prompt_file),
                      facts=lambda: facts)
        return ex, orders

    def decision() -> dict:
        return calls and DECISIONS[-1] or {}

    DECISIONS: list[dict] = []
    real = complete

    def complete_logged(system, user, **kw):
        result = real(system, user, **kw)
        if isinstance(result, dict) and "go" in result:
            DECISIONS.append(result)
        return result

    complete = complete_logged  # noqa: F811 — запоминаем решения исследователя

    def show(name: str, d: dict, orders: Orders) -> None:
        print(f"== {name}")
        for key in ("say", "why", "plan", "talk", "ask", "learned", "note"):
            if d.get(key):
                print(f"  {key}: {d[key]}")
        print(f"  пошёл: {orders.sent[-1] if orders.sent else '—'}\n")

    cases = [args.case] if args.case else ["start", "hint", "talk", "mood"]
    if "start" in cases or "hint" in cases:
        game = Game(PEOPLE, HEARD)
        ex, orders = explorer(game)
        ex.heard("Походи по Вайтрану, поисследуй город, пообщайся с его жителями.")
        ex._begin("explore", "Хорошо, пройдусь по Вайтрану, поговорю с людьми.")
        show("Старт: «походи по Вайтрану» (разговор уже ответил «пройдусь, поговорю с людьми»)", decision(), orders)
        if "hint" in cases:
            ex.hint("Саадия просит не выдавать её: её ищут и хотят убить; встреча с ними — у Конюшен Вайтрана.")
            ex._thought("тебе пришла мысль: Саадия — редгардка; а в «Времени лишений» ищут женщину-редгардку. "
                        "Спросить бы её")
            show("Мысль про Саадию + подсказка из разговора Владоса", decision(), orders)

    def talk_case(title: str, npc: Nearby, opener: str, mood: str, touched_words: tuple[str, ...]):
        spoken = []

        def feel(_name, text):
            hit = [w for w in touched_words if w in text.lower()]
            return f"это задевает тебя лично (тебя это пугает: «{hit[0]}»)" if hit else ""

        talk = Talk(cfg.talk, Game([npc], HEARD), complete, deliver=lambda t: spoken.append(("Гослинг", t)) or True,
                    say_as=lambda f, n, v, r, t: spoken.append((n, t)) or True,
                    samples=NS(edid=lambda v: "", refs=lambda e: []), quotes_path=ROOT / "datasets" / "game_quotes.tsv",
                    people=lambda name: "; ".join(t for k, t in knowledge.search(name, 4)
                                                  if k == "person" and name.split()[0] in t),
                    model=cfg.explorer.model, manner=manner(prompt_file, without=("Игрока зовут",)),
                    me=lambda: "\n".join(x for x in (soul, mood) if x), feel=feel)
        talk.converse(npc, opener, "узнать о городе и о нём самом")
        print(f"== {title}")
        for who, text in spoken:
            print(f"  {who}: {text}")
        print()

    if "talk" in cases:
        talk_case("Разговор с Хеймскром о драконах (душа: боится драконов)", PEOPLE[0],
                  "Слушай, а Талос защитит нас, если дракон прилетит в Вайтран?", mood_line, ("дракон",))
    if "mood" in cases:
        talk_case("Разговор с Белетором, Гослинг подавлен", PEOPLE[4], "Привет. Что у тебя сегодня хорошего?",
                  "Твоё настроение сейчас (из того, что было): подавлен — еле отбились от бандитов у Ривервуда, ты ранен", ())

    print("Запросы:", "; ".join(f"{t[:40]} — {n} симв., {s:.1f} с" for t, n, s in calls))
    return 0


if __name__ == "__main__":
    sys.exit(main())
