"""Приказы Гослингу голосом (кусок 6 плана): поле "do" ответа модели -> команда плагину -> игра.

Модель не двигает Гослинга, а решает, что сделать; плагин зовёт скрипт GoslingOrders (Papyrus), тот — ванильный
квест спутника DialogueFollower, как диалог «Жди здесь» / «Следуй за мной» / «Нам пора расстаться». Движением и
боем управляет игра; отпущенный (dismiss) идёт пешком к своему месту в таверне — его обычный пакет поведения.

go_to — сходить к месту с карты Владоса: название из ответа модели -> открытый маркер карты (данные игры, не догадка)
-> пакет ИИ «путешествуй к…» на алиасе квеста похода (GoslingOrders.GoTo); дойдя — ждать там или вернуться (back).
Новый приказ поход отменяет. Пока идёт — «дело» в «Сейчас в игре»
(GameState.task), дошёл — событие arrived.

Результат — событие order_result. Вышло — состояние видно в «Сейчас в игре» по данным игры (gosling.waiting,
gosling.follower); не вышло — повод мозгу: Гослинг обещал, а не сделал, и скажет почему.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from companion import trace
from companion.game.geo import Marker, distance_m, format_distance
from companion.game.places import Spot
from companion.game.state import Change, GameState, ItemNear

log = logging.getLogger(__name__)

ACTIONS = {"wait": "остаться ждать на месте", "follow": "пойти за Владосом",
           "dismiss": "расстаться с Владосом и уйти к себе", "go_to": "сходить к месту",
           "explore": "уйти исследовать самому", "lead": "исследовать, ведя Владоса за собой",
           "talk": "поговорить с человеком рядом"}
EXPLORE = ("explore", "lead")  # вылазки исследователя (companion/explorer.py): ходит сам, куда решит
# Коды отказа -> причина для модели. Прочие (no_quest, papyrus, ...) — сбой мода.
REASONS = {
    "not_follower": "ты сейчас не спутник Владоса — сначала он должен позвать тебя с собой",
    "other_follower": "у Владоса уже есть другой спутник, а двоих игра не берёт — пусть сначала отпустит его",
    "dead": "ты мёртв",
    "no_path": "дорогу туда ты не нашёл — застрял или пути нет",
    "no_place": "такого места нет на карте Владоса",
    "too_far": "это слишком далеко — так далеко один ты пока не ходишь",
    "locked": "сундук заперт, а вскрывать замки ты не умеешь",
    "gone": "человек ушёл туда, куда тебе за ним не пройти (в дом или далеко)",
}
CANCELLED = "cancelled"  # поход отменил новый приказ — не неудача: что он делает теперь, решил новый приказ
# go_to: дальше — не идём. Ведёт пакет ИИ «путешествуй к…» (квест похода в Gosling.esp) — его игра ведёт и за
# загруженной областью (PathToReference туда не ходил: 578 м — «дошёл», сдвинувшись на метр, 21.10). Предел — чтобы
# не уходил через полкарты на несколько игровых часов; подбираем по игре.
GO_MAX = 3000  # м


@dataclass
class Order:
    action: str
    place: str = ""   # go_to: название места, как на карте
    back: bool = False
    started: float = 0.0  # time.time() — когда отдан: сколько шёл и что было по дороге
    marker: Marker | None = None  # приказ исследователя: к какому маркеру (go_to) или какой нанести на карту (map)
    explorer: bool = False  # отдал исследователь, а не Владос — ответ ему (on_explorer_result)
    item: ItemNear | None = None  # take: что взять


class Orders:
    def __init__(self, send_command: Callable[[dict], None], plugin_connected: Callable[[], bool], game: GameState):
        self.send_command = send_command
        self.plugin_connected = plugin_connected
        self.game = game
        self._pending: dict[str, Order] = {}  # id команды -> приказ
        self._lock = threading.Lock()
        # Не вышло ещё до игры (места нет на карте) — события туда же, куда и события игры (ставит сервер).
        self.on_changes: Callable[[list[Change]], None] = lambda changes: None
        # Исследователь (companion/explorer.py, ставит сервер): начать вылазку; приказ Владоса — вылазке конец;
        # ответ игры на приказ самого исследователя -> его события.
        self.on_explore: Callable[[str, str], None] = lambda mode, said: None
        self.on_order: Callable[[], None] = lambda: None
        self.on_explorer_result: Callable[[Order, dict], list[Change]] = lambda order, data: []
        # «Сходи туда», а вылазка идёт: место — её следующая цель (True — исследователь взял), а не конец вылазки.
        self.on_redirect: Callable[[Marker], bool] = lambda marker: False
        # «Поговори с ним»: подойти, поговорить и вернуться рассказать — делает исследователь (подход и разговор — его).
        self.on_talk: Callable[[str, str], None] = lambda who, opener: None
        game.on_order_result = self._on_result

    def give(self, action: str, place: str = "", back: bool = False, said: str = "", who: str = "",
             opener: str = "") -> None:
        """Приказ Владоса (action — из ACTIONS). Ответ игры придёт событием order_result. said — что Гослинг ответил,
        соглашаясь: начиная вылазку, исследователь держится сказанного («пойдём к шахте» — первая цель шахта)."""
        if action not in ACTIONS:
            return
        if action in EXPLORE:
            self.on_explore(action, said)
            return
        if action == "talk":  # не приказ «вместо» прежнего: идёт вылазка — разговор в её ходе, она не кончается
            self.on_talk(who, opener)
            return
        if not self.plugin_connected():
            log.warning("Приказ %s: игра не на связи — не отдан", action)
            trace.event(f"Приказ не отдан: игра не на связи ({ACTIONS[action]})")
            return
        command = {"type": "order", "action": action}
        order = Order(action)
        if action == "go_to":
            marker, problem = self._target(place)
            if not marker:
                self.on_changes(self._failed(Order(action, place), problem))
                return
            if not back and self.on_redirect(marker):
                trace.event(f"Приказ «сходить к месту «{marker.name}»» — следующей целью вылазки")
                return
            order = Order(action, marker.name, back, time.time())
            command |= {"target": marker.form, "stay": not back}
            self.game.task = f"идёшь к месту «{marker.name}» по просьбе Владоса" + (", потом назад к нему" if back else "")
            self.game.task_done = False
        # Владос решил иначе — вылазке конец. Только когда приказ уходит в игру: там скрипт снимает и сам поход
        # (Order -> StopTravel, GoTo — новая цель). Не ушёл (места нет на карте) — вылазка идёт дальше, иначе поход
        # в игре оставался без хозяина: вёл к хижине, хотя вылазка «кончилась» (21.10).
        self.on_order()
        self._send(order, command)

    # ------------------------------------------------------------ приказы исследователя

    # Возвращают номер команды: по нему исследователь узнаёт ответ игры на свой приказ.

    def go_marker(self, marker: Marker, lead: bool) -> str:
        """Исследователь: к маркеру (любому — и неоткрытому), дойдя — ждать там; lead — ведя Владоса."""
        return self._send(Order("go_to", marker.name, False, time.time(), marker, explorer=True),
                          {"type": "order", "action": "go_to", "target": marker.form, "stay": True, "lead": lead})

    def go_spot(self, spot: Spot, name: str, lead: bool) -> str:
        """Исследователь: внутрь — к особой точке локации из файлов игры (companion/game/places.py); плагин найдёт
        ссылку по файлу и номеру. Пакеты проходят через двери."""
        return self._send(Order("go_to", name, False, time.time(), explorer=True),
                          {"type": "order", "action": "go_to", "file": spot.file, "local": spot.local, "stay": True,
                           "lead": lead})

    def go_ref(self, form: int, name: str, lead: bool, arrive: float = 0) -> str:
        """Исследователь: к ссылке в игре — дверь здания снаружи («у входа»), дойдя — ждать там; человек (arrive — м,
        дошёл, когда ближе) — скрипт идёт за ним, пока тот ходит, и, дойдя, держится рядом до следующего приказа
        (release — отойти и ждать на месте)."""
        return self._send(Order("go_to", name, False, time.time(), explorer=True),
                          {"type": "order", "action": "go_to", "target": form, "stay": True, "lead": lead}
                          | ({"arrive": arrive} if arrive else {}))

    def mark(self, marker: Marker) -> str:
        """Исследователь: нанести маркер на карту Владоса (нашёл неоткрытое место)."""
        return self._send(Order("map", marker.name, marker=marker, explorer=True),
                          {"type": "order", "action": "map", "target": marker.form})

    def take(self, item: ItemNear) -> str:
        """Исследователь: взять ничью вещь рядом или забрать всё из сундука (кроме квестового — его оставит)."""
        return self._send(Order("take", item.name, explorer=True, item=item),
                          {"type": "order", "action": "take", "target": item.form})

    def hand_over(self, form: int, count: int, name: str) -> str:
        """Исследователь: отдать Владосу находку вылазки (базовый предмет, сколько)."""
        return self._send(Order("give", name, explorer=True),
                          {"type": "order", "action": "give", "item": form, "count": count})

    def command(self, action: str) -> str:
        """Исследователь: простой приказ (follow — вылазка кончилась, назад к Владосу; unstick — застрял, выбраться к
        нему; release — больше не держаться рядом с человеком (не бродить тут), ждать на месте; linger — побыть здесь:
        постоять, посидеть, пока не отпустят), вылазку не останавливая."""
        return self._send(Order(action, explorer=True), {"type": "order", "action": action})

    def _send(self, order: Order, command: dict) -> str:
        command_id = uuid.uuid4().hex[:8]
        with self._lock:
            self._pending[command_id] = order
        log.info("Приказ %s: %s", command_id, order)
        trace.event(f"Приказ {command_id}{' (исследователь)' if order.explorer else ''}: {self._what(order)}")
        self.send_command(command | {"id": command_id})
        return command_id

    def _target(self, place: str) -> tuple[Marker | None, str]:
        """Маркер карты по названию из ответа модели — только открытые Владосом места. -> (маркер, код отказа)."""
        name = place.strip().casefold()
        known = [m for m in self.game.markers if m.visible and m.form]
        marker = (next((m for m in known if m.name.casefold() == name), None)
                  or next((m for m in known if name and name in m.name.casefold()), None))
        if not marker:
            log.info("go_to: «%s» нет среди открытых мест карты", place)
            return None, "no_place"
        pos = self.game.status.pos  # в помещении позиции нет — расстояние не проверить, пробуем
        if pos and (d := distance_m(pos[0], pos[1], marker)) > GO_MAX:
            log.info("go_to: «%s» в %s — дальше %d м", marker.name, format_distance(d), GO_MAX)
            return None, "too_far"
        return marker, ""

    @staticmethod
    def _what(order: Order) -> str:
        if order.action == "map":
            return f"нанести на карту место «{order.place}»"
        if order.action == "take":
            return f"взять «{order.place}»"
        if order.action == "give":
            return f"отдать Владосу «{order.place}»"
        if order.action == "unstick":
            return "выбраться к Владосу оттуда, где застрял"
        if order.action == "release":
            return "отойти от человека и ждать на месте"
        if order.action == "linger":
            return "побыть здесь и послушать"
        if order.action != "go_to":
            return ACTIONS.get(order.action, "сделать, что он просил")
        return f"сходить к месту «{order.place}»" + (" и вернуться" if order.back else " и ждать там")

    def _failed(self, order: Order, code: str) -> list[Change]:
        self.game.task = None
        reason = REASONS.get(code, f"сбой мода ({code or 'без причины'})")
        text = f"ты пообещал Владосу {self._what(order)}, но не вышло: {reason}"
        trace.event(f"Приказ не выполнен: {self._what(order)} — {code}")
        return [Change("order_failed", text, subject=text, important=True, brief=f"не вышло {self._what(order)}")]

    def _on_result(self, data: dict) -> list[Change]:
        with self._lock:
            order = self._pending.pop(str(data.get("id")), None) or Order("")
        if order.explorer:
            trace.event(f"Приказ {data.get('id')} (исследователь): {self._what(order)} — "
                        + ("выполнен" if data.get("ok") else str(data.get("reason"))))
            return self.on_explorer_result(order, data)
        if data.get("reason") == CANCELLED:
            trace.event(f"Приказ {data.get('id')} отменён новым: {self._what(order)}")
            return []
        if not data.get("ok"):
            return self._failed(order, str(data.get("reason") or ""))
        trace.event(f"Приказ {data.get('id')} выполнен: {self._what(order)}")
        if order.action != "go_to":
            self.game.task = None  # новый приказ отменяет прежнее дело; ждёт или идёт — видно по данным игры
            return []
        then = "возвращаешься к Владосу" if order.back else "ждёшь там Владоса"
        self.game.task, self.game.task_done = f"дошёл до места «{order.place}», {then}", True
        # Что было в дороге — только его собственные события («глаза Гослинга»): нет драк — так и скажем, иначе модель
        # выдумывает приключения («чуть волка не испугался», 21.10).
        minutes = max(1, round((time.time() - order.started) / 60)) if order.started else None
        fights = [c for t, c in self.game.recent if t >= order.started and c.kind == "own_fight"]
        road = (f"шёл {minutes} мин; " if minutes else "") + (
            f"по дороге дрался: {len(fights)} раз" if fights
            else "по дороге ни с кем не дрался — ничего не выдумывай про приключения в пути")
        return [Change("arrived", f"ты дошёл до места «{order.place}» ({road}) и {then}",
                       subject=f"дошёл: {order.place}", important=True, brief=f"дошёл до места «{order.place}»",
                       where=order.place)]
