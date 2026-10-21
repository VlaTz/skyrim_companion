"""Приказы Гослингу голосом (кусок 6 плана): поле "do" ответа модели -> команда плагину -> игра.

Модель не двигает Гослинга, а решает, что сделать; плагин зовёт скрипт GoslingOrders (Papyrus), тот — ванильный
квест спутника DialogueFollower, как диалог «Жди здесь» / «Следуй за мной» / «Нам пора расстаться». Движением и
боем управляет игра; отпущенный (dismiss) идёт пешком к своему месту в таверне — его обычный пакет поведения.

go_to — сходить к месту с карты Владоса: название из ответа модели -> открытый маркер карты (данные игры, не догадка)
-> Actor.PathToReference к нему; дойдя — ждать там или вернуться (back). Пока идёт — «дело» в «Сейчас в игре»
(GameState.task), дошёл — событие arrived.

Результат — событие order_result. Вышло — состояние видно в «Сейчас в игре» по данным игры (gosling.waiting,
gosling.follower); не вышло — повод мозгу: Гослинг обещал, а не сделал, и скажет почему.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Callable

from companion import trace
from companion.game.geo import Marker, distance_m, format_distance
from companion.game.state import Change, GameState

log = logging.getLogger(__name__)

ACTIONS = {"wait": "остаться ждать на месте", "follow": "пойти за Владосом",
           "dismiss": "расстаться с Владосом и уйти к себе", "go_to": "сходить к месту"}
# Коды отказа -> причина для модели. Прочие (no_quest, papyrus, ...) — сбой мода.
REASONS = {
    "not_follower": "ты сейчас не спутник Владоса — сначала он должен позвать тебя с собой",
    "other_follower": "у Владоса уже есть другой спутник, а двоих игра не берёт — пусть сначала отпустит его",
    "dead": "ты мёртв",
    "no_path": "дорогу туда ты не нашёл — застрял или пути нет",
    "no_place": "такого места нет на карте Владоса",
    "too_far": "это слишком далеко — пока ты ходишь только к местам поблизости",
}
# go_to: дальше — не идём. PathToReference ходит только в загруженной области вокруг игрока: к Ривервуду за 578 м
# через 27 с вернул «дошёл», сдвинув Гослинга на метр (21.10). Скрипт GoslingOrders.GoTo проверяет то же (~140 м) от
# Гослинга, здесь — от игрока, чтобы не гонять приказ в игру зря. Дальше — нужен пакет ИИ «путешествуй к…».
GO_MAX = 140  # м


@dataclass
class Order:
    action: str
    place: str = ""   # go_to: название места, как на карте
    back: bool = False


class Orders:
    def __init__(self, send_command: Callable[[dict], None], plugin_connected: Callable[[], bool], game: GameState):
        self.send_command = send_command
        self.plugin_connected = plugin_connected
        self.game = game
        self._pending: dict[str, Order] = {}  # id команды -> приказ
        self._lock = threading.Lock()
        # Не вышло ещё до игры (места нет на карте) — события туда же, куда и события игры (ставит сервер).
        self.on_changes: Callable[[list[Change]], None] = lambda changes: None
        game.on_order_result = self._on_result

    def give(self, action: str, place: str = "", back: bool = False) -> None:
        """Отдать приказ игре (action — из ACTIONS). Ответ придёт событием order_result."""
        if action not in ACTIONS:
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
            order = Order(action, marker.name, back)
            command |= {"target": marker.form, "stay": not back}
            self.game.task = f"идёшь к месту «{marker.name}» по просьбе Владоса" + (", потом назад к нему" if back else "")
            self.game.task_done = False
        command_id = uuid.uuid4().hex[:8]
        with self._lock:
            self._pending[command_id] = order
        log.info("Приказ %s: %s", command_id, order)
        trace.event(f"Приказ {command_id}: {self._what(order)}")
        self.send_command(command | {"id": command_id})

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
        if not data.get("ok"):
            return self._failed(order, str(data.get("reason") or ""))
        trace.event(f"Приказ {data.get('id')} выполнен: {self._what(order)}")
        if order.action != "go_to":
            self.game.task = None  # новый приказ отменяет прежнее дело; ждёт или идёт — видно по данным игры
            return []
        then = "возвращаешься к Владосу" if order.back else "ждёшь там Владоса"
        self.game.task, self.game.task_done = f"дошёл до места «{order.place}», {then}", True
        return [Change("arrived", f"ты дошёл до места «{order.place}» и {then}", subject=f"дошёл: {order.place}",
                       important=True, brief=f"дошёл до места «{order.place}»")]
