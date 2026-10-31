"""Мозг компаньона: когда заговорить самому.

Получает изменения в игре (companion/game/state.py), реплики NPC рядом (подслушанное) и раз в секунду
проверяет тишину. Говорить ли — решает модель: на каждый повод она пишет, что сказала бы, и ставит interest
0–10 (шкала одна для всех поводов); Гослинг говорит, если interest не ниже brain.interest. Модель видит, когда
и что Гослинг сам сказал последним и идёт ли бой, — и сама решает, не болтает ли он подряд. Разговорчивость —
одной цифрой, без пауз и кубиков.

Код только не даёт перебивать себя: пока Гослинг говорит или отвечает игроку, поводы копятся и уходят модели
одной пачкой, когда он освободится; между вопросами модели — не меньше brain.ask_gap секунд (чтобы не
спрашивать её на каждый чих). Подслушанное ждёт, пока NPC договорят и игрок выйдет из меню разговора.
"""

from __future__ import annotations

import logging
import queue
import random
import threading
import time
from typing import Callable

from companion import trace
from companion.config import BrainConfig
from companion.game.state import Change, GameState
from companion.voice.service import VoiceService

log = logging.getLogger(__name__)

# Шкала — общая для событий, подслушанного и тишины; examples — что для этого повода значит «важно вам».
SCALE = ("Оцени, стоит ли это твоей реплики прямо сейчас, — поле interest от 0 до 10 в том же JSON:\n"
         "{bands}"
         "9–10 — нельзя промолчать: впервые увиденное, тяжёлое ранение или гибель Владоса, твоё тяжёлое ранение, "
         "внезапная беда, которую "
         "он может не заметить; обычный бой — не 9–10.\n"
         "Оценивай сам этот повод, а не обстановку в целом. {recency} Если ты только что говорил (меньше минуты "
         "назад) или идёт бой — ставь не выше 6, кроме 9–10: подряд болтать незачем, в бою — не до разговоров.\n"
         "В text — что бы ты сказал: 1–2 коротких предложения от себя, всего не больше 25 слов. А если ставишь "
         "ниже {threshold} — всё равно напиши в text, но не серьёзную реплику, а шутку или подколку к месту одной "
         "фразой, не больше 10 слов (скажешь её не всегда). "
         'Формат: {{"interest": 0..10, "emotion": "...", "text": "..."{joke}}} — interest обязателен.')
JOKE_FIELD = ', "joke": "<id заготовки или строки истории, если обыграл её>"'
EVENT_BANDS = ("0–2 — обыденное: вошли в помещение, открыл сундук, подобрал мелочь, очередной бой с обычными "
               "врагами;\n"
               "3–4 — можно бы, но незачем;\n"
               "5–6 — есть что сказать к месту, но можно и промолчать;\n"
               "7–8 — важно или интересно вам: новое задание или цель, знакомый человек, новое место, находка, "
               "враг вдали, поступок Владоса, о котором есть что сказать;\n")
OVERHEARD_BANDS = ("0–2 — фон: работа, хозяйство, торг, приветствия, обычная болтовня горожан;\n"
                   "3–4 — любопытно, но не про вас;\n"
                   "5–6 — есть что сказать, но можно и промолчать: чья-то беда или ссора, спор о войне, "
                   "политике, богах;\n"
                   "7–8 — касается вас: ваше задание, его люди и находки (в том числе разговор Владоса с NPC "
                   "о деле), награда, слух о месте или человеке; или по-настоящему смешно;\n")
IDLE_BANDS = ("0–4 — сейчас не до разговоров: бой, напряжённый момент, Владос занят делом;\n"
              "5–6 — сказать совсем нечего;\n"
              "7–8 — долгая тишина сама по себе повод: есть хоть что-то к месту — байка или воспоминание о том, где "
              "вы были, вопрос Владосу, мысль о том, что вокруг;\n")
EVENT_PROMPT = "Игрок ничего не говорил. Только что: {what}.\n"
OUTING_HINT = """Ты — Гослинг, картограф и спутник Владоса в Скайриме; сейчас ты в вылазке (куда идти, решаешь не здесь).
Рядом только что говорили — ты слышал. Выбери из этого то, что важно для вылазки: кто этот человек и чем занят, слух,
место, задание, кого стоит расспросить или куда сходить. Только из услышанного, без догадок. Дежурные фразы
(приветствия, «не создавай проблем», насмешки, болтовня о погоде) — не важное. О неважном не пиши; ничего важного — пусто.
Отвечай строго одним JSON-объектом: {"hint": "<одна-две фразы или пусто>"}"""
OVERHEARD_PROMPT = ("Рядом только что говорили (ты слышал):\n{lines}\n"
                    "Оценивай именно эти реплики, а не обстановку в целом; не пересказывай услышанное — "
                    "игрок слышал то же самое.\n")
IDLE_PROMPT = "Вы уже давно идёте молча ({silence}). Можешь заговорить сам.\n"
# Мысль (companion/memory/thinker.py): говорить ли и когда, решил мыслитель — шкалы нет, модель только говорит.
THOUGHT_PROMPT = ("Тебе пришла мысль, которую стоит сказать Владосу {when}: {thought}\n"
                  "Скажи её своими словами, от себя и к месту: 1–2 коротких предложения, всего не больше 25 слов.")
# Размышление о мире (companion/musings.py): повод — запись «общеизвестного» из игры; говорить ли — решает модель.
MUSING_PROMPT = ("Игрок ничего не говорил, вы давно идёте молча ({silence}). Тебе вдруг пришла в голову мысль о "
                 "мире — повод из того, что знает каждый местный: «{topic}»\n"
                 "Если зацепило — порассуждай вслух от себя: удивление, вопрос Владосу, догадка или шутливая теория "
                 "(вроде «ты вот никогда не думал, почему…»). Не пересказывай повод как справку — оттолкнись от "
                 "него. Догадки — как догадки («а может…», «вот я думаю…»): не выдавай за правду о мире то, чего "
                 "в поводе нет. 1–2 коротких предложения. Не зацепило или сейчас не к месту — оставь text пустым.")
THOUGHT_NOW = "сейчас"
THOUGHT_IDLE = "— самое время, раз вы давно идёте молча ({silence})"
HEARD_MAX_AGE = 30    # подслушанное старше минуты (Гослинг всё это время был занят) — не комментируем
DIALOGUE_STALE = 60  # меню разговора «открыто», а реплик нет 2 минуты — видимо, пропустили его закрытие
GAME_STALE = 20       # секунд без событий из игры — игра не запущена или висит на загрузке
# Срочное — держится до brain.fresh_important, даже если не помечено важным: тяжело ранен, Крик, впервые увиденное.
URGENT = {"low_health", "power", "novel", "died", "hurt_self", "order_failed", "talked", "oddity"}
# Поводы, которые Владос адресовал лично Гослингу: на них он отвечает, как на обращение, — без шкалы интереса.
PERSONAL = {"gift", "taken", "hired", "dismissed"}
PERSONAL_PROMPT = ("Это Владос сделал лично тебе — ответь ему, как если бы он к тебе обратился: от себя, к месту, "
                   "1–2 коротких предложения, всего не больше 25 слов.")
# Приказ голосом не вышел (companion/orders.py): Гослинг обещал — должен сказать, что не сделал, без шкалы.
ORDER_FAILED_PROMPT = ("Ты только что пообещал это Владосу, а сделать не вышло — скажи ему об этом от себя: "
                       "одно короткое предложение, почему.")
# Поговорил с человеком по просьбе Владоса (companion/explorer.py, _converse_errand): тот ждёт рассказа — без шкалы.
TALKED_PROMPT = ("Владос просил тебя поговорить с этим человеком — ты поговорил. Расскажи ему коротко, от себя, что "
                 "узнал: 1–2 предложения, только то, что тот сказал (его словами — его мнение, не факт); ничего не "
                 "добавляй. Где ты знаешь иначе (пометка «Ты знаешь иначе»), — не передавай как правду, скажи, что "
                 "не веришь или знаешь иначе. Ничего толком не сказал — так и передай.")
# Событие задело запись души (companion/personal.py): повод сказать от себя — по обычной шкале (не к месту — промолчит).
PERSONAL_TOUCH = ("Это задевает тебя лично — так написано в твоей душе, ты сам это о себе решил. Если правда зацепило, "
                  "скажи от себя, как своё (страх, радость, воспоминание), а не как справку о событии, и оцени interest "
                  "не ниже 7; не к месту — оцени ниже.\n")
# Переписка (companion/letters.py): Гослинг далеко — сказать не может, но может написать. Пишет по своим важным событиям
# и когда долго ждёт (brain.letter_idle); всё остальное вдали — молча.
LETTER_KINDS = {"arrived", "own_fight", "own_fight_end", "own_unknown", "hurt_self", "order_failed"}
LETTER_EVENT_PROMPT = ("Владос далеко, вы переписываетесь — вслух ты ему не скажешь, можешь только написать. Только что "
                       "с тобой: {what}.\nЕсли это стоит письма — напиши Владосу коротко, от себя, как записку: 1–2 "
                       "предложения. Не стоит — оставь text пустым.")
LETTER_IDLE_PROMPT = ("Владос далеко уже {minutes} мин, вы переписываетесь — вслух ты ему не скажешь. Можешь написать ему "
                      "короткую записку от себя (1–2 предложения): как ты, где ты, что вокруг, спросить, как он. Нечего "
                      "писать — оставь text пустым.")
# Идёт вылазка (companion/explorer.py) — об этом говорит исследователь: мозг молчит, иначе говорят двое (21.10: в
# «Веди» оба комментировали «новая локация», «мародёр впереди»). «Веди» — место, вход, открытие; мысли и тишина в
# пути — мозгу: исследователь говорит только при решениях, и между ними Гослинг шёл молча (21.10); что решил —
# мозг видит в блоке «Вылазка». Один — письма «отбился» и «ранен»: на них исследователь решает заново и пишет сам.
# Мысли в вылазке — исследователю (companion/explorer.py, _thought): мозг говорил «спрошу её», а исследователь шёл к
# другому (24.10); за мысль берётся тот, кто решает, — и говорит о ней сам.
# Бой, рана Владоса — мозгу.
EXPLORER_TALKS = {"lead": {"location", "wilderness", "interior", "discovered", "thought"},
                  "explore": {"own_fight_end", "hurt_self", "thought"}}
THOUGHT = "мысль: "  # Change.subject мысли — «мысль: <текст>»
LOADED = {"loaded", "died"}  # загрузка сейва (died — после гибели Владоса): всё, что было до неё, — другое прошлое
TICK = 0.25           # с — как часто мозг смотрит, не пора ли (раньше 1 с — до секунды лишней задержки)


class Brain:
    def __init__(self, cfg: BrainConfig, voice: VoiceService, game: GameState):
        self.cfg = cfg
        self.voice = voice
        self.game = game
        self._changes: queue.Queue[Change] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="brain")
        self.last_ask = 0.0                      # когда последний раз спрашивали модель, заговорить ли
        self._idle_checked = 0.0                 # когда последний раз спрашивали её на тишину
        self._commented: dict[str, float] = {}   # место или текст события -> когда показывали модели
        self._heard: list[Change] = []           # подслушанное с прошлого решения
        self._heard_at = 0.0                     # когда пришла последняя реплика NPC
        self._heard_lock = threading.Lock()
        # Мысли «при случае» — от мыслителя (Thinker.unsaid / Thinker.said): высказать на долгую тишину.
        self.unsaid_thought: Callable[[], str | None] = lambda: None
        self.thought_said: Callable[[str], None] = lambda text: None
        self.jokes = None  # companion.jokes.Jokes — заготовки шуток к обстановке (нет базы — без них)
        self.musings = None  # companion.musings.Musings — темы для размышлений в тишине (нет — без них)
        self._mused_at = 0.0  # когда последний раз пробовали размышление
        self._letter_at = 0.0  # когда Гослинг вдали последний раз писал сам
        self.exploring: Callable[[], str] = lambda: ""  # вылазка: "lead" | "explore" | "" (ставит сервер)
        self.outing_hint: Callable[[str], None] = lambda text: None  # важное из услышанного — исследователю

    def start(self) -> None:
        self._thread.start()
        log.info("Мозг: сам заговаривает, если модель оценит повод не ниже %d (тишина — %d с)",
                 self.cfg.interest, self.cfg.idle_after)

    def stop(self) -> None:
        self._stop.set()

    def notify(self, changes: list[Change]) -> None:
        """Вызывается из обработчика /game/event."""
        if any(c.kind in LOADED for c in changes):
            # Загрузили сейв: подслушанное и «об этом уже думал» — из другого прошлого.
            with self._heard_lock:
                self._heard = []
            self._commented = {}
        for change in changes:
            if change.kind == "overheard":
                with self._heard_lock:
                    self._heard.append(change)
                    self._heard_at = time.time()
            else:
                self._changes.put(change)

    # ------------------------------------------------------------ цикл

    def _run(self) -> None:
        pending: list[tuple[float, Change]] = []  # (когда пришло, что)
        first_at = 0.0
        while not self._stop.is_set():
            try:
                pending.append((time.time(), self._changes.get(timeout=TICK)))
                first_at = first_at or time.time()
                continue  # сначала забираем всё, что пришло пачкой
            except queue.Empty:
                pass
            now = time.time()
            # Пока Гослинг говорит, поводы ждут — но обстановка уходит: обычное старше brain.fresh_for, важное —
            # старше brain.fresh_important уже не про то, что сейчас («вошли в лавку», а вы уже вышли).
            stale = [c for t, c in pending
                     if now - t > (self.cfg.fresh_important if c.important or c.kind in URGENT else self.cfg.fresh_for)]
            if stale:
                self._silent(stale, "пока был занят, повод устарел")
                pending = [(t, c) for t, c in pending if c not in stale]
                first_at = min((t for t, _c in pending), default=0.0)
            # Связанные события (вошли в город + сменилось помещение) ждём settle секунд и решаем разом.
            if pending and now - first_at >= self.cfg.settle:
                if self._on_changes([c for _t, c in pending], now):
                    pending, first_at = [], 0.0
            elif not pending:
                self._maybe_idle(now)
            self._maybe_overheard(now)

    def _free(self, now: float) -> bool:
        """Можно спросить модель: Владос жив, Гослинг не говорит и не отвечает, его услышат (он рядом), и с прошлого
        вопроса прошло ask_gap. Далеко — не готовим и повод (шутки к нему, размышления): сказать всё равно не сможет."""
        return (not self.game.dead and not self.voice.busy() and now - self.last_ask >= self.cfg.ask_gap
                and self.voice.can_speak())

    def _on_changes(self, changes: list[Change], now: float) -> bool:
        """False — Гослинг занят, события стоит придержать."""
        loaded = max((i for i, c in enumerate(changes) if c.kind in LOADED), default=0)
        if loaded:  # загрузили сейв: что копилось до этого (и мысли), уже про другое прошлое
            del changes[:loaded]
        if muted := EXPLORER_TALKS.get(self.exploring(), set()):
            quiet = [c for c in changes if c.kind in muted]
            if quiet:
                self._silent(quiet, "идёт вылазка — об этом скажет исследователь")
                changes = [c for c in changes if c not in quiet]
        shown = [c for c in changes if c.kind in self.cfg.events]
        # Одно и то же место недавно уже показывали — не повторяемся.
        fresh = [c for c in shown if now - self._commented.get(c.subject or c.text, 0) > self.cfg.repeat_after]
        if not fresh:
            if shown:
                self._silent(shown, "об этом уже думал недавно")
            return True
        if not self.voice.can_speak():  # Гослинг далеко: вслух нельзя — важное своё пишет письмом, остальное молча
            return self._letter(fresh, now)
        thoughts = [c for c in fresh if c.subject.startswith(THOUGHT)]
        if thoughts and self.game.in_combat:  # в бою не до мыслей — подождут тишины (Thinker.unsaid)
            self._silent(thoughts, "идёт бой — мысль подождёт тишины")
            fresh, thoughts = [c for c in fresh if c not in thoughts], []
            if not fresh:
                return True
        if not self._free(now):
            return False
        for c in fresh:
            self._commented[c.subject or c.text] = now
        events = [c for c in fresh if c not in thoughts]
        what = "; ".join(c.text for c in events)
        if any(c.kind == "order_failed" for c in events):
            log.info("Мозг: приказ не вышел -> %s", what)
            self._ask(EVENT_PROMPT.format(what=what) + ORDER_FAILED_PROMPT, now, changes=events)
            return True
        if any(c.kind == "talked" for c in events):
            log.info("Мозг: поговорил по просьбе -> %s", what)
            self._ask(EVENT_PROMPT.format(what=what) + TALKED_PROMPT, now, changes=events)
            return True
        if any(c.kind in PERSONAL for c in events) and not self.game.in_combat:
            # Владос сделал что-то лично Гослингу (отдал вещь, забрал) — это как обращение к нему: отвечает без шкалы.
            # По шкале «только что говорил — не выше 6» глушило: щит отдали через 18 с после реплики — интерес 3–4.
            log.info("Мозг: лично тебе -> %s", what)
            self._ask(EVENT_PROMPT.format(what=what) + PERSONAL_PROMPT, now, changes=events)
            return True
        if thoughts:
            # Мысль «сейчас»: решил мыслитель — не переоцениваем (шкала ставила своей же мысли 3–4). Одна за раз,
            # остальные — на тишину.
            thought = thoughts[0].subject.removeprefix(THOUGHT)
            log.info("Мозг: мысль -> %s", thought)
            if self._ask((EVENT_PROMPT.format(what=what) if events else "Игрок ничего не говорил.\n")
                         + THOUGHT_PROMPT.format(when=THOUGHT_NOW, thought=thought), now):
                self.thought_said(thought)
            return True
        log.info("Мозг: событие -> %s", what)
        touch = PERSONAL_TOUCH if any(c.kind == "personal" for c in events) else ""
        self._ask(EVENT_PROMPT.format(what=what) + touch, now, EVENT_BANDS, changes=events)
        return True

    def _letter(self, changes: list[Change], now: float) -> bool:
        """Гослинг далеко: о своём важном (LETTER_KINDS) — письмо Владосу. False — занят, событие придержать."""
        events = [c for c in changes if c.kind in LETTER_KINDS]
        if not events or not self.voice.can_write():
            self._silent(changes, "Гослинг далеко — вслух не скажет, а писать не о чем")
            return True
        if self.game.dead or self.voice.busy():
            return False
        for c in events:
            self._commented[c.subject or c.text] = now
        self.last_ask = self._letter_at = now
        what = "; ".join(c.text for c in events)
        log.info("Мозг: письмо Владосу -> %s", what)
        self.voice.initiate(LETTER_EVENT_PROMPT.format(what=what), letter=True)
        return True

    def _maybe_letter_idle(self, now: float) -> None:
        """Гослинг далеко и давно молчит (brain.letter_idle с расставания или прошлого письма) — записка Владосу."""
        apart = self.game.apart_since
        if (apart is None or self.game.dead or self.voice.busy() or not self.voice.can_write()
                or now - max(apart, self._letter_at) < self.cfg.letter_idle):
            return
        self._letter_at = self.last_ask = now
        log.info("Мозг: долго порознь — записка Владосу")
        self.voice.initiate(LETTER_IDLE_PROMPT.format(minutes=round((now - apart) / 60)), letter=True)

    def _maybe_overheard(self, now: float) -> None:
        with self._heard_lock:
            if not self._heard or now - self._heard_at < self.cfg.overheard_quiet:
                return  # разговор ещё идёт
            if now - self._heard_at > HEARD_MAX_AGE:
                self._heard = []  # так и не дождались удобного момента — уже неактуально
                return
            if self.game.in_dialogue and now - self._heard_at < DIALOGUE_STALE:
                return  # игрок ещё говорит с NPC — не встреваем, ждём конца разговора
            if not self._free(now):
                return  # Гослинг говорит или только что спрашивали — решим чуть позже
            lines, self._heard = self._heard, []
        if self.exploring():
            # В вылазке не встревает в разговоры (24.10: комментировал каждую фразу и объявлял, куда идти), а отбирает
            # из них важное для вылазки — исследователю, выжимкой.
            threading.Thread(target=self._outing_hint, args=(lines,), daemon=True, name="outing-hint").start()
            return
        log.info("Мозг: подслушал %d реплик — встрять ли?", len(lines))
        self._ask(OVERHEARD_PROMPT.format(lines="\n".join(c.text for c in lines)), now, OVERHEARD_BANDS)

    def _outing_hint(self, lines: list) -> None:
        """Что важного для вылазки в услышанном — одной-двумя фразами исследователю (companion/explorer.py, hint)."""
        llm = getattr(self.voice, "llm", None)
        if not llm:
            return
        result = llm.complete_json(OUTING_HINT, f"Вылазка: {self.game.outing or 'идёт'}\nУслышанное:\n"
                                   + "\n".join(c.text for c in lines), title="Мозг: важное для вылазки")
        if isinstance(result, dict) and (hint := str(result.get("hint") or "").strip()):
            self.outing_hint(hint)

    def _maybe_idle(self, now: float) -> None:
        # Тишина — с последней реплики (его или игрока), а не с последнего вопроса «заговорить ли»: в городе мозг
        # спрашивает модель о болтовне стражников каждые несколько секунд, и тишина иначе не наступала никогда.
        if not self.voice.can_speak():  # далеко — не тишина, а разлука: изредка записка
            self._maybe_letter_idle(now)
            return
        anchor = max(self.voice.last_activity, self._idle_checked)
        if now - anchor < self.cfg.idle_after or not self._free(now):
            return
        self._idle_checked = now
        if self.game.updated is None or now - self.game.updated > GAME_STALE:
            return
        quiet = now - anchor
        silence = f"{round(quiet)} с" if quiet < 90 else f"{round(quiet / 60)} мин"
        # Есть мысль «при случае», ещё не сказанная, — тишина и есть случай: говорим её, а не «придумай что-нибудь».
        if not self.game.in_combat and (thought := self.unsaid_thought()):
            log.info("Мозг: долгая тишина — скажу мысль: %s", thought)
            if self._ask("Игрок ничего не говорил.\n" + THOUGHT_PROMPT.format(
                    when=THOUGHT_IDLE.format(silence=silence), thought=thought), now):
                self.thought_said(thought)
            return
        if self._maybe_muse(now, silence):
            return
        log.info("Мозг: долгая тишина")
        self._ask(IDLE_PROMPT.format(silence=silence), now, IDLE_BANDS, extra_tags={"тишина"})

    def _maybe_muse(self, now: float, silence: str) -> bool:
        """Долгая тишина: иногда — размышление о мире (не чаще brain.musings.gap). True — спрашивали модель."""
        cfg = self.cfg.musings
        if (not cfg.enabled or self.musings is None or self.game.in_combat or now - self._mused_at < cfg.gap
                or random.random() >= cfg.chance):
            return False
        s = self.game.status
        topic = self.musings.topic(" ".join(p for p in (s.location, s.hold) if p))
        if not topic:
            return False
        self._mused_at = now
        log.info("Мозг: долгая тишина — размышление: %s", topic[:80])
        self.last_ask = now
        scene = self._scene()
        if self.voice.initiate(MUSING_PROMPT.format(silence=silence, topic=topic),
                               still_fresh=lambda: self._scene() == scene,
                               reasoning=cfg.reasoning, max_tokens=cfg.max_tokens if cfg.reasoning else None):
            self.musings.said(topic)
        else:
            self.musings.skipped(topic)
        return True

    def _ask(self, situation: str, now: float, bands: str | None = None, changes: list[Change] | None = None,
             extra_tags: set[str] | None = None) -> bool:
        """Спросить модель, что сказать. bands — шкала интереса: говорит, только если оценка не ниже brain.interest;
        None — без шкалы (мысль: говорить ли, уже решено). changes — повод: по нему и обстановке подбираются
        заготовки шуток (companion/jokes.py). True — сказал."""
        self.last_ask = now
        scene = self._scene()
        if bands is None:
            return self.voice.initiate(situation, still_fresh=lambda: self._scene() == scene)
        # Неважное (ниже порога) — иногда всё-таки короткой шуткой: не в бою и не раньше quip_gap после прошлой
        # шутки (раньше считали от любой реплики — и в городе шутки не звучали вовсе).
        quip = (0.0 if self.game.in_combat or now - self.voice.last_joke_at < self.cfg.quip_gap
                else self.cfg.quip_chance)
        jokes = ""
        if self.jokes is not None and not self.game.in_combat:
            try:
                jokes = self.jokes.block(changes, extra_tags)
            except Exception:  # noqa: BLE001 — без шуток, но не без реплики
                log.exception("Шутки: не подобрать")
        return self.voice.initiate(situation + jokes + SCALE.format(bands=bands, recency=self._recency(now),
                                                                    threshold=self.cfg.interest,
                                                                    joke=JOKE_FIELD if jokes else ""),
                                   min_interest=self.cfg.interest, still_fresh=lambda: self._scene() == scene,
                                   quip_chance=quip)

    def _scene(self) -> tuple:
        """Обстановка, при смене которой реплика по своей инициативе уже не к месту: место, помещение, бой, гибель.
        Пока модель думала и синтезировался голос (~3–4 с), игрок мог выйти из лавки, влезть в драку или погибнуть
        («12 процентов, выпей зелье» — уже мёртвому)."""
        s = self.game.status
        return s.location, s.cell, bool(s.in_combat), self.game.dead

    def _recency(self, now: float) -> str:
        """Что модели знать о себе, чтобы решить, не болтает ли она подряд."""
        llm = self.voice.llm
        said = (f"Ты сам говорил {round(now - llm.last_reply_at)} с назад: «{llm.last_reply_text}»."
                if llm and llm.last_reply_at else "Ты пока ничего не говорил.")
        return said + (" Сейчас идёт бой." if self.game.in_combat else "")

    def _silent(self, changes: list[Change], reason: str) -> None:
        """В трассу: почему модель даже не спросили. Только о том, что вообще показываем ей (не о злокрысах)."""
        could = [c.text for c in changes if c.kind in self.cfg.events]
        if could:
            trace.event(f"Мозг промолчал: {'; '.join(could)[:200]} — {reason}")
