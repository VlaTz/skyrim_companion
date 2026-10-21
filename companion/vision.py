"""Зрение Гослинга: кадр игры — модели с картинками («глаза»), чтобы Гослинг видел то, чего нет в событиях и данных:
вид, погоду, обстановку, что у Владоса в руках, надписи.

Своей камеры у NPC нет — кадр снимается с экрана, то есть камерой Владоса: Гослинг идёт рядом и видит то же самое.
Поэтому смотрит, только когда он рядом (vision.max_distance) и на экране мир — не меню, не пауза, не загрузка
(status.paused из плагина), а окно игры — на переднем плане. Окно лога F10 в кадр не попадает (overlay.py,
WDA_EXCLUDEFROMCAPTURE).

Два способа:
- сам — на события vision.glance_on (новое место, новое место на карте, смена погоды) и в тишине (vision.silence):
  в фоне, разговор не ждёт; описание — в обстановку «Что вы видите вокруг», пока свежее и вы на том же месте;
- по просьбе — инструмент look(вопрос): «видишь башню?», «что у меня в руках?».

Правда: «глаза» говорят только о видимом, названия — только из данных игры (их дают вместе с кадром), спор кадра и
данных — в пользу данных. Последние кадры — logs/frames (vision.frames), в трассе — что по ним сказано.
Настройки — секция vision.
"""

from __future__ import annotations

import base64
import ctypes
import io
import logging
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Callable

from companion import trace
from companion.config import VisionConfig
from companion.game.state import Change, GameState

log = logging.getLogger(__name__)

SKYRIM_WINDOW = "Skyrim Special Edition"  # и класс, и заголовок окна игры
DPI_PER_MONITOR = ctypes.c_void_p(-4)    # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 — координаты в пикселях экрана
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
NOT_VISIBLE = "мира не видно"
# Из обстановки — «глазам» только то, что помогает узнать увиденное: где, когда, кто рядом.
CONTEXT_LINES = ("Место:", "Время:", "Ты сам", "Боя нет", "Идёт бой", "Рядом:", "Враги")
TICK = 5  # с — как часто проверять тишину
BLACK = b""  # grab: кадр почти чёрный — экран загрузки или затемнение (в «глаза» не шлём)
BLACK_MEAN, BLACK_SPREAD = 10, 8  # яркость 0..255 в среднем и разброс: ниже обоих — «чёрный»
LOADED = {"loaded", "died"}  # загрузка сейва
PLACE_KINDS = {"location", "wilderness"}  # сменилось место — вернулись быстро, вид тот же

SYSTEM = f"""Ты — глаза Гослинга, спутника Владоса в Скайриме. Перед тобой кадр игры — вид камерой Владоса; Гослинг
идёт рядом и видит то же самое.
Как читать кадр:
- Интерфейс игры — не мир, о нём не говори вовсе: компас вверху, полоски здоровья, магии и запаса сил внизу, прицел,
  подсказки клавиш, субтитры. Прочитать субтитр или надпись — только если о них спрашивают.
- Камера — Владоса, а не Гослинга: Гослинг — отдельный персонаж, он идёт рядом и может попасть в кадр, но смотрим не
  его глазами. Вид от третьего лица: фигура в центре кадра, обычно спиной, — Владос. Вид от первого лица: руки и оружие
  на переднем плане — руки Владоса, что бы ни было в данных о снаряжении Гослинга.
- Меню, карта, инвентарь, экран загрузки, чёрный экран — мира не видно: ответь ровно «{NOT_VISIBLE}».
Правда:
- Говори только о том, что видно на кадре. Не додумывай, что за кадром, внутри зданий и сундуков.
- Названия — только из данных игры ниже (место, кто рядом). Чего в данных нет — общими словами («каменная башня»,
  «большой зверь»), не придумывай имён.
- Кадр и данные спорят — верь данным; не уверен, что это, — так и скажи.
Пиши по-русски, коротко, без вступлений."""
GLANCE = ("Опиши, что вокруг, в 2–3 предложениях: что за местность или помещение, вид и даль, погода и свет, что "
          "примечательного (существа, люди, постройки, необычное). Не перечисляй всё подряд.")
LOOK = "Вопрос: {question}\nОтветь по кадру одной-двумя фразами. Не видно на кадре — так и скажи."

user32 = ctypes.WinDLL("user32")  # свой экземпляр: типы функций ниже не трогают окно лога
for _name in ("FindWindowW", "GetForegroundWindow", "SetThreadDpiAwarenessContext"):
    getattr(user32, _name).restype = ctypes.c_void_p  # дескрипторы и указатели — 64 бита, не int
user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.IsIconic.argtypes = [ctypes.c_void_p]
user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.POINT)]


def _game_window() -> int:
    return user32.FindWindowW(SKYRIM_WINDOW, None) or user32.FindWindowW(None, SKYRIM_WINDOW)


def grab(width: int, quality: int) -> bytes | None:
    """Кадр окна игры (JPEG, не шире width) или None: окна нет, свёрнуто или не на переднем плане (тогда на экране
    не игра); BLACK — кадр почти чёрный (загрузка при переходе в другую ячейку, затемнение)."""
    from PIL import ImageGrab, ImageStat

    hwnd = _game_window()
    if not hwnd or user32.IsIconic(hwnd) or user32.GetForegroundWindow() != hwnd:
        return None
    old = user32.SetThreadDpiAwarenessContext(DPI_PER_MONITOR)  # только этот поток: окно лога не трогаем
    try:
        rect, origin = wintypes.RECT(), wintypes.POINT(0, 0)
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        user32.ClientToScreen(hwnd, ctypes.byref(origin))
        vx, vy = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        screen = ImageGrab.grab(all_screens=True)  # весь рабочий стол — с левого верхнего угла (vx, vy)
    finally:
        user32.SetThreadDpiAwarenessContext(old)
    if rect.right <= 0 or rect.bottom <= 0:
        return None
    left, top = origin.x - vx, origin.y - vy
    frame = screen.crop((left, top, left + rect.right, top + rect.bottom)).convert("RGB")
    stat = ImageStat.Stat(frame.convert("L").resize((64, 36)))
    if stat.mean[0] < BLACK_MEAN and stat.stddev[0] < BLACK_SPREAD:
        return BLACK
    if frame.width > width:
        frame = frame.resize((width, round(frame.height * width / frame.width)))
    buf = io.BytesIO()
    frame.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def ago(seconds: float) -> str:
    return "только что" if seconds < 20 else f"{round(seconds)} с назад" if seconds < 90 else \
        f"{round(seconds / 60)} мин назад"


class Vision:
    def __init__(self, cfg: VisionConfig, game: GameState, client, frames_dir: Path, *, model: str,
                 extra_body: dict | None = None, last_activity: Callable[[], float] = lambda: 0.0):
        self.cfg = cfg
        self.game = game
        self.client = client  # OpenAI-совместимый клиент основной модели (OpenRouter)
        self.model = cfg.model or model
        self.extra_body = cfg.extra_body if cfg.extra_body is not None else extra_body  # обычно — как у ответов
        self.frames_dir = frames_dir
        self.last_activity = last_activity  # когда последний раз кто-то говорил — тишина
        self._lock = threading.Lock()
        self._busy = threading.Lock()  # взгляд сам — один за раз; look ждёт не его, а модель
        self._seen: tuple[str, float, tuple] | None = None  # описание, когда, где
        self._last_glance = 0.0
        self._stop = threading.Event()
        self._loaded_at = 0.0  # загрузка сейва: сразу после неё на экране ещё загрузка — не смотрим
        self._here: tuple | None = None  # где были при прошлом событии
        self._left: dict[tuple, float] = {}  # место -> когда ушли оттуда (вернулись быстро — заново не смотрим)

    # ------------------------------------------------------------ можно ли смотреть

    def _place(self) -> tuple:
        s = self.game.status
        return s.location, bool(s.interior), s.cell if s.interior else ""

    def blind(self) -> str:
        """Почему Гослинг сейчас не видит того, что видно Владосу; "" — видит."""
        g = self.game.status.gosling
        if not g or not g.loaded or g.dead or g.distance < 0 or g.distance > self.cfg.max_distance:
            return "Ты сейчас не рядом с Владосом — не видишь, что у него перед глазами."
        if self.game.status.paused:
            return "Сейчас не видно: у Владоса открыто меню или идёт загрузка."
        return ""

    # ------------------------------------------------------------ смотреть

    def _frame(self) -> bytes | None:
        """Кадр мира или None: окна нет или кадр чёрный (загрузка, затемнение) — такой в «глаза» не шлём."""
        frame = grab(self.cfg.width, self.cfg.quality)
        if frame == BLACK:
            log.info("Зрение: кадр чёрный (загрузка) — не смотрим")
            return None
        return frame

    def _see(self, reason: str, task: str, frame: bytes | None) -> str | None:
        """Кадр -> «глаза». Текст ответа или None (кадра нет или мира не видно)."""
        t = time.perf_counter()
        if frame is None:
            return None
        saved = self._save(frame, reason)
        data = "\n".join(line for line in self.game.describe().splitlines() if line.startswith(CONTEXT_LINES))
        # «Ты сам (Гослинг)» глаза «Гослинга» читали как «камера — моя»: «в руках Гослинга топор».
        data = data.replace("Ты сам (Гослинг):", "Гослинг, идёт рядом (камера не его; снаряжение — его, не Владоса):")
        block = trace.Block(f"Глаза: {reason} [{self.model}]")
        block.section("Кадр", f"{saved or 'не сохранён'} ({len(frame) // 1024} КБ)")
        block.section("Данные игры", data or "нет")
        block.section("Задача", task)
        try:
            resp = self.client.chat.completions.create(
                model=self.model, temperature=0.2, max_tokens=self.cfg.max_tokens,
                messages=[{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": [
                              {"type": "text", "text": f"Данные игры:\n{data or 'нет'}\n\n{task}"},
                              {"type": "image_url", "image_url": {
                                  "url": "data:image/jpeg;base64," + base64.b64encode(frame).decode()}}]}],
                extra_body=self.extra_body or None)
            text = (resp.choices[0].message.content or "").strip()
        except Exception as e:  # noqa: BLE001 — не увидел — не беда, разговор идёт дальше
            log.warning("Зрение: модель не ответила: %s", e)
            block.line(f"ОШИБКА: {e!r}")
            block.write()
            return None
        block.section("Увидел", text)
        block.line(f"──── {time.perf_counter() - t:.2f} с")
        block.write()
        log.info("Зрение (%s) %.1f с: %s", reason, time.perf_counter() - t, text[:200])
        return None if not text or NOT_VISIBLE in text.lower() else text

    def look(self, question: str = "") -> str:
        """Инструмент: посмотреть по просьбе — ответ на вопрос по кадру."""
        if why := self.blind():
            return why
        task = LOOK.format(question=question.strip() or "что вокруг?")
        text = self._see("по просьбе", task, self._frame())
        self._last_glance = time.time()
        return text or "Сейчас не видно: на экране не мир (меню, загрузка или игра свёрнута)."

    def glance(self, reason: str) -> None:
        """Взглянуть самому, в фоне: описание — в обстановку."""
        if self.blind() or not self._busy.acquire(blocking=False):
            return
        try:
            place = self._place()
            time.sleep(self.cfg.settle)  # событие о новом месте приходит, пока на экране ещё загрузка
            if self._place() != place or self.blind():  # пока ждали, ушли или Гослинг отстал
                return
            when = time.time()
            text = self._see(reason, GLANCE, self._frame())
            self._last_glance = time.time()
            if text and self._place() == place:  # пока смотрел, не ушли
                with self._lock:
                    self._seen = (text, when, place)
        finally:
            self._busy.release()

    def notify(self, changes: list[Change]) -> None:
        """События игры: на vision.glance_on — взглянуть (новое место — только под открытым небом). Не смотрим сразу
        после загрузки сейва (vision.after_load: на экране ещё загрузка) и туда, откуда ушли меньше vision.revisit
        назад (граница локации: «вошли — ушли — вошли» — один и тот же вид)."""
        now, place = time.time(), self._place()
        if any(c.kind in LOADED for c in changes):
            self._loaded_at, self._here, self._left = now, place, {}
        elif place != self._here:
            if self._here is not None:
                self._left[self._here] = now
            self._here = place
        kinds = {c.kind for c in changes if c.kind in self.cfg.glance_on}
        if not kinds or now - self._loaded_at < self.cfg.after_load:
            return
        if kinds & PLACE_KINDS and now - self._left.get(place, 0.0) < self.cfg.revisit:
            log.info("Зрение: вернулись сюда меньше %d с назад — не смотрим", self.cfg.revisit)
            kinds -= PLACE_KINDS
        if kinds - {"location"} or ("location" in kinds and not self.game.status.interior):
            reason = next(c.gist for c in changes if c.kind in kinds)
            threading.Thread(target=self.glance, args=(reason,), daemon=True, name="vision").start()

    # ------------------------------------------------------------ в обстановку

    def describe(self) -> str:
        """Строка в обстановку: что вы видите вокруг, если описание свежее и вы на том же месте; иначе ""."""
        with self._lock:
            seen = self._seen
        if not seen or time.time() - seen[1] > self.cfg.fresh or seen[2] != self._place() or self.blind():
            return ""
        return (f"Что вы видите вокруг (взглянул {ago(time.time() - seen[1])}): {seen[0]} "
                "Можешь сказать об этом, если к месту; без повода вид не описывай.")

    # ------------------------------------------------------------ тишина

    def start(self) -> None:
        log.info("Зрение: %s, сам — на %s и в тишине (%d с), рядом — до %d м", self.model,
                 ", ".join(self.cfg.glance_on), self.cfg.silence, self.cfg.max_distance)
        threading.Thread(target=self._run, daemon=True, name="vision-silence").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(TICK):
            now = time.time()
            if (now - self.last_activity() >= self.cfg.silence and now - self._last_glance >= self.cfg.silence_gap
                    and now - self._loaded_at >= self.cfg.after_load and not self.game.in_combat
                    and not self.describe()):
                try:
                    self.glance("тишина")
                except Exception:  # noqa: BLE001
                    log.exception("Зрение: ошибка")

    # ------------------------------------------------------------ кадры для разбора

    def _save(self, frame: bytes, reason: str) -> str:
        if self.cfg.frames <= 0:
            return ""
        try:
            self.frames_dir.mkdir(parents=True, exist_ok=True)
            safe = "".join(ch if ch.isalnum() else "_" for ch in reason)[:30]
            path = self.frames_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{safe}.jpg"
            path.write_bytes(frame)
            for old in sorted(self.frames_dir.glob("*.jpg"))[:-self.cfg.frames]:
                old.unlink(missing_ok=True)
            return path.name
        except OSError as e:
            log.warning("Зрение: кадр не сохранён: %s", e)
            return ""
