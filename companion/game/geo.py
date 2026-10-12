"""Ориентация на местности: маркеры карты мира, расстояния и стороны света.

Плагин присылает все маркеры карты (map_markers) и позицию игрока (status.pos). Гослинг видит
только то, что игрок уже открыл на карте, плюс неизвестные места совсем рядом — без названия,
только тип («какая-то пещера в 400 м на север»): иначе миру нечего было бы открывать.
"""

from __future__ import annotations

import math

from pydantic import BaseModel

UNITS_PER_METER = 70.0  # единица длины Скайрима ≈ 1,43 см

# Типы маркеров карты по номеру (RE::MARKER_TYPE в CommonLibSSE-NG).
MARKER_TYPES = {
    0: "место", 1: "город", 2: "городок", 3: "поселение", 4: "пещера", 5: "лагерь", 6: "форт",
    7: "нордские руины", 8: "двемерские руины", 9: "место кораблекрушения", 10: "роща",
    11: "приметное место", 12: "логово дракона", 13: "ферма", 14: "лесопилка", 15: "шахта",
    16: "лагерь имперцев", 17: "лагерь Братьев Бури", 18: "камень-хранитель", 19: "мельница",
    20: "плавильня", 21: "конюшни", 22: "сторожевая башня", 23: "поляна", 24: "перевал",
    25: "алтарь", 26: "скала", 27: "маяк", 28: "орочья крепость", 29: "лагерь великанов",
    30: "хижина", 31: "нордская башня", 32: "нордское жилище", 33: "пристань", 34: "святилище",
    53: "храм", 54: "поселение", 55: "камень-хранитель", 56: "башня Телванни", 57: "пристань",
    58: "пристань", 59: "замок",
}
for _n in range(35, 53):  # замки и столицы владений идут парами: замок, город
    MARKER_TYPES[_n] = "замок" if (_n - 35) % 2 == 0 else "город"

DIRECTIONS = ["север", "северо-восток", "восток", "юго-восток", "юг", "юго-запад", "запад", "северо-запад"]


class Marker(BaseModel):
    name: str
    type: int = 0
    x: float
    y: float
    visible: bool = False  # есть на карте игрока
    travel: bool = False   # открыт (можно быстро переместиться)

    @property
    def kind(self) -> str:
        return MARKER_TYPES.get(self.type, "место")


def distance_m(px: float, py: float, m: Marker) -> float:
    return math.hypot(m.x - px, m.y - py) / UNITS_PER_METER


def direction(px: float, py: float, m: Marker) -> str:
    """Сторона света от игрока к месту: в Скайриме +Y — север, +X — восток."""
    angle = math.degrees(math.atan2(m.x - px, m.y - py)) % 360
    return DIRECTIONS[round(angle / 45) % 8]


def format_distance(meters: float) -> str:
    if meters < 1000:
        return f"~{max(50, round(meters / 50) * 50)} м"
    return f"~{meters / 1000:.1f} км".replace(".", ",")


def find_places(px: float, py: float, markers: list[Marker], kind: str, name: str, max_m: float,
                unknown_radius: float, limit: int) -> list[str]:
    """Поиск для инструмента агента: открытые места по типу/названию/расстоянию,
    плюс неоткрытые совсем рядом (без названия — их игрок ещё не нашёл)."""
    kind, name = kind.strip().lower(), name.strip().lower()
    found = []
    for m in sorted(markers, key=lambda m: distance_m(px, py, m)):
        d = distance_m(px, py, m)
        if (kind and kind not in m.kind) or (max_m and d > max_m):
            continue
        where = f"{format_distance(d)} на {direction(px, py, m)}"
        if m.visible:
            if not name or name in m.name.lower():
                found.append(f"{m.name} ({m.kind}) — {where}")
        elif not name and d <= unknown_radius:
            found.append(f"неизвестное место ({m.kind}, нет на карте) — {where}")
        if len(found) >= limit:
            break
    return found


def describe_surroundings(px: float, py: float, markers: list[Marker], here: str | None,
                          known_count: int, unknown_radius: float, unknown_count: int) -> list[str]:
    """Строки для промпта: ближайшие открытые места и неизвестное рядом."""
    by_distance = sorted(markers, key=lambda m: distance_m(px, py, m))
    known = [m for m in by_distance if m.visible and m.name != here][:known_count]
    unknown = [m for m in by_distance
               if not m.visible and distance_m(px, py, m) <= unknown_radius][:unknown_count]
    lines = []
    if known:
        lines.append("Знакомые места на карте: " + "; ".join(
            f"{m.name} ({m.kind}) — {format_distance(distance_m(px, py, m))} на {direction(px, py, m)}"
            for m in known) + ".")
    if unknown:
        lines.append("Рядом что-то неизвестное, чего нет на карте: " + "; ".join(
            f"{m.kind} — {format_distance(distance_m(px, py, m))} на {direction(px, py, m)}"
            for m in unknown) + ".")
    return lines
