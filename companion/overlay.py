"""Окно с логом поверх игры: что сейчас «в голове» у Гослинга — без сворачивания Скайрима.

Полупрозрачное, всегда сверху, клики проходят сквозь него и фокус у игры оно не забирает. Показывает то же, что
консоль start.bat (без сетевого шума), с подсветкой: мозг, модель, мыслитель, летописец, предупреждения.
Клавиша overlay.key (по умолчанию F10) — показать / спрятать.

Видно только поверх игры в окне без рамки (SkyrimPrefs.ini: bFull Screen=0, bBorderless=1) — поверх
эксклюзивного полного экрана Windows ничего не рисует.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading

from companion.config import OverlayConfig

log = logging.getLogger(__name__)

# Окно Windows: прозрачное для мыши, без кнопки на панели задач, не забирает фокус у игры, поверх всех.
GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x8000000
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
TOPMOST_EVERY = 2000  # мс — заново поднять поверх (игра в окне без рамки иногда перекрывает)
POLL_EVERY = 150      # мс — забрать новые строки лога

QUIET_LOGGERS = ("httpx", "httpcore", "urllib3", "uvicorn", "openai", "asyncio")  # сетевой шум — не показываем
COLORS = {  # по началу имени логгера — цвет строки
    "companion.brain": "#ffd75f",
    "companion.llm": "#87d7ff",
    "companion.memory.thinker": "#87ff87",
    "companion.memory.diarist": "#d7afff",
    "companion.memory": "#bcbcbc",
    "companion.voice": "#ffafd7",
}
WARN_COLOR = "#ff8700"
TEXT_COLOR = "#e4e4e4"


class _QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__(logging.INFO)
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(QUIET_LOGGERS):
            return
        try:
            self.q.put_nowait((record.name, record.levelno, self.format(record)))
        except queue.Full:
            pass


class Overlay:
    def __init__(self, cfg: OverlayConfig):
        self.cfg = cfg
        self._lines: queue.Queue = queue.Queue(maxsize=2000)
        self._toggle = threading.Event()

    def start(self) -> None:
        logging.getLogger().addHandler(_QueueHandler(self._lines))
        threading.Thread(target=self._run, daemon=True, name="overlay").start()
        self._listen_key()
        log.info("Окно с логом поверх игры: %s — показать/спрятать (видно в окне без рамки)", self.cfg.key.upper())

    def _listen_key(self) -> None:
        from pynput import keyboard

        from companion.voice.audio_io import parse_key

        key = parse_key(self.cfg.key)

        def on_press(pressed) -> None:
            if pressed == key or (isinstance(key, keyboard.KeyCode) and isinstance(pressed, keyboard.KeyCode)
                                  and (pressed.char or "").lower() == key.char):
                self._toggle.set()

        keyboard.Listener(on_press=on_press, daemon=True).start()

    # ------------------------------------------------------------ окно (всё — в своём потоке: Tk однопоточный)

    def _run(self) -> None:
        import tkinter as tk

        c = self.cfg
        root = tk.Tk()
        root.overrideredirect(True)
        root.configure(bg="black")
        screen_w = root.winfo_screenwidth()
        x = screen_w - c.width - c.margin if c.corner.endswith("right") else c.margin
        y = c.margin if c.corner.startswith("top") else root.winfo_screenheight() - c.height - c.margin
        root.geometry(f"{c.width}x{c.height}+{x}+{y}")
        text = tk.Text(root, bg="black", fg=TEXT_COLOR, font=("Consolas", c.font_size), wrap="word",
                       borderwidth=0, highlightthickness=0, padx=6, pady=4)
        text.pack(fill="both", expand=True)
        for name, color in COLORS.items():
            text.tag_configure(name, foreground=color)
        text.tag_configure("warn", foreground=WARN_COLOR)
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
                              | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
        root.attributes("-alpha", c.alpha)  # после смены стиля: прозрачность ставится заново
        root.attributes("-topmost", True)
        visible = c.visible
        if not visible:
            root.withdraw()

        def raise_top() -> None:
            if visible:
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
            root.after(TOPMOST_EVERY, raise_top)

        def poll() -> None:
            nonlocal visible
            if self._toggle.is_set():
                self._toggle.clear()
                visible = not visible
                if visible:
                    root.deiconify()
                    root.attributes("-alpha", c.alpha)
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
                else:
                    root.withdraw()
            added = False
            while True:
                try:
                    name, level, line = self._lines.get_nowait()
                except queue.Empty:
                    break
                tag = "warn" if level >= logging.WARNING else next(
                    (n for n in COLORS if name.startswith(n)), "")
                text.insert("end", line + "\n", tag)
                added = True
            if added:
                extra = int(text.index("end-1c").split(".")[0]) - c.lines
                if extra > 0:
                    text.delete("1.0", f"{extra + 1}.0")
                text.see("end")
            root.after(POLL_EVERY, poll)

        root.after(POLL_EVERY, poll)
        root.after(TOPMOST_EVERY, raise_top)
        root.mainloop()
