"""Душа Гослинга: что он решил о себе — какое оружие и броню хочет, что любит есть, чего боится, о чём мечтает,
привычки и мнения. Растёт из разговоров, а не сочиняется заранее: корень характера — prompt.md (его правит игрок).

Отвечает Гослинг сразу, как обычно, — разговор не ждёт. После главы летописца отдельный фоновый запрос (сильная модель
с рассуждением: решение «кто я» стоит обдумать) смотрит, что Гослинг в этой главе сказал о себе, и решает, что из
этого — часть его самого: добавить, заменить (передумал) или убрать. С описанием характера и уже записанным не спорит.
Душа — в памяти по сейву (store.soul — журнал изменений) и всегда в промпте: о себе Гослинг говорит в согласии с ней.
Настройки — секция soul в config.yaml.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from companion.config import SoulConfig
from companion.memory.store import MemoryStore, Where

log = logging.getLogger(__name__)

PERSONA_END = "ПАМЯТЬ"  # описание характера в prompt.md — до раздела памяти

SYSTEM = """Ты — Гослинг, спутник Владоса в Скайриме. Ниже — описание твоего характера, твоя «душа» (что ты уже решил
о себе) и записи последнего разговора. Найди, что ты в этих записях сказал о себе всерьёз и надолго: что любишь и не
любишь (оружие, броня, еда, питьё, места, занятия), чего боишься, о чём мечтаешь, привычки, мнения, что-то из своего
прошлого — и реши, стоит ли это запомнить как часть себя.
- Только то, что ты сам сказал о себе (реплики «Ты (Гослинг)»). Нового о себе здесь не придумывай.
- Сказанное в шутку, для красного словца или мимоходом, не всерьёз, — не запоминай.
- С описанием характера не спорь: противоречит ему — не запоминай.
- Противоречит записанному в душе — значит, ты передумал: замени запись (тот же key) или убери её (пустой text),
  но только если в записях видно, что передумал всерьёз; иначе ничего не записывай.
- Уже записанное, только другими словами, — не записывай.
- Что у тебя сейчас надето, в руках или в сумке, — не душа: это меняется и видно в игре. Душа — вкусы и отношение
  (что любишь, что тебе удобно или нет), а не то, что на тебе сейчас.
Перед ответом проверь каждую запись: говорит ли она, что на тебе или у тебя сейчас (что носишь, надел, держишь, что в
сумке)? Тогда оставь в ней только вкус, который за этим стоит, без того, что на тебе, — или убери запись совсем.
key — о чём, коротко, в нижнем регистре. Вкусы — по одному key на тему: «оружие», «броня», «еда», «питьё»; остальное —
«страх: …», «мечта: …», «привычка: …», «мнение: …», «прошлое: …», «любит: …», «не любит: …».
text — от первого лица, одной фразой, как ты сам сказал бы о себе. why — что ты сказал и в ответ на что, коротко.
items — каждое блюдо, напиток, оружие и броня, названные в text, — как названы, даже если название описательное
(«суп из …», «… с …»); не перечисляй только совсем общие слова (мясо, горячая еда, топор, лёгкая броня); нет таких —
пустой список.
feel — поводы для настроения: что в событиях игры тебя по этой записи обрадует, напугает, вызовет неприязнь или
грусть, — короткие слова, по которым такое событие узнать (существо, предмет, место, занятие, погода), в начальной
форме: {"радость": [...], "страх": [...], "неприязнь": [...], "грусть": [...]}; только чувства, которые прямо есть
в записи, — не додумывай; нечего — пустой объект.
Повод срабатывает на любое событие игры с этим словом, поэтому он должен узнавать только то, что правда задевает
запись: конкретное существо, вещь по виду или названию («секира», «морозный паук»), место по названию. Общие слова из
обычных событий (карта, место, новое место, открытие, бой, дорога, оружие, броня, щит, еда, рубить) — не поводы:
«на карте появилось новое место» бывает постоянно и не исполняет мечту о месте, которого нет ни на одной карте. Не
узнать такое событие конкретным словом — у записи поводов нет.
Место, работа или время из прошлого — не повод для страха и неприязни: «боюсь вернуться картографом в Солитьюд»
или «в Солитьюде было скучно» — воспоминание, а не страх перед самим городом; событие «вошли в Солитьюд» ничего из этого
не значит. Такое слово в страх и неприязнь не пиши (радость — если запись прямо говорит, что место тебя радует).
Перед ответом проверь каждое слово: случится в игре событие с ним — правда ли ты это почувствуешь? Названное в записи
мимоходом — для сравнения, украшения, как место из прошлого — не повод (дракон, нарисованный на полях карты, — не
встреча с драконом; город, где было скучно, сам по себе не неприятен). Нет — убери слово.
У записей души, где поводов нет («поводы: нет»), верни их с тем же key и text — только с feel.
Отвечай строго одним JSON-объектом без пояснений и markdown:
{"soul": [{"key": "...", "text": "...", "why": "...", "items": [...], "feel": {...}}]} — обычно пустой список."""

FEELINGS = ("радость", "страх", "неприязнь", "грусть")


def clean_feel(feel) -> dict[str, list[str]]:
    """Поводы из ответа модели: только известные чувства и непустые слова."""
    if not isinstance(feel, dict):
        return {}
    out = {}
    for name in FEELINGS:
        words = [str(w).strip().lower() for w in feel.get(name) or [] if str(w).strip()] if isinstance(
            feel.get(name), list) else []
        if words:
            out[name] = words
    return out


def feel_text(feel: dict[str, list[str]] | None) -> str:
    return "; ".join(f"{name}: {', '.join(words)}" for name, words in (feel or {}).items()) or "нет"


# Поводы к записям души, сделанным раньше (до поводов): общий запрос их не дописывал — отдельный короткий.
FEEL_SYSTEM = """Ты — Гослинг, спутник Владоса в Скайриме. Ниже — записи твоей души (что ты решил о себе). Для каждой
напиши поводы для настроения: что в событиях игры тебя по этой записи обрадует, напугает, вызовет неприязнь или грусть,
— короткие слова, по которым такое событие узнать (существо, предмет, место, занятие, погода), в начальной форме.
Только чувства, которые прямо есть в записи, — не додумывай (что связано с прошлым или скучным делом, ещё не
значит, что неприятно само место); нечего — пустой объект.
Повод срабатывает на любое событие игры с этим словом, поэтому он должен узнавать только то, что правда задевает
запись: конкретное существо, вещь по виду или названию («секира», «морозный паук»), место по названию. Общие слова из
обычных событий (карта, место, новое место, открытие, бой, дорога, оружие, броня, щит, еда, рубить) — не поводы:
«на карте появилось новое место» бывает постоянно и не исполняет мечту о месте, которого нет ни на одной карте. Не
узнать такое событие конкретным словом — у записи поводов нет.
Место, работа или время из прошлого — не повод для страха и неприязни: «боюсь вернуться картографом в Солитьюд»
или «в Солитьюде было скучно» — воспоминание, а не страх перед самим городом; событие «вошли в Солитьюд» ничего из этого
не значит. Такое слово в страх и неприязнь не пиши (радость — если запись прямо говорит, что место тебя радует).
Перед ответом проверь каждое слово: случится в игре событие с ним — правда ли ты это почувствуешь? Названное в записи
мимоходом — для сравнения, украшения, как место из прошлого — не повод (дракон, нарисованный на полях карты, — не
встреча с драконом; город, где было скучно, сам по себе не неприятен). Нет — убери слово.
Отвечай строго одним JSON-объектом без пояснений и markdown:
{"feel": {"<номер записи>": {"радость": [...], "страх": [...], "неприязнь": [...], "грусть": [...]}, ...}}"""

# Названный в душе предмет, которого в игре нет («рагу из медведя с картошкой»), — запись переписать без него.
FIX_SYSTEM = """Ты — Гослинг, спутник Владоса в Скайриме. В записи о себе ты назвал предметы, которых в Скайриме нет.
Перепиши запись, сохранив смысл: замени их общим словом (что это за еда, питьё, оружие); настоящим предметом из
списка похожих — только если это по сути то же самое, другое блюдо или вещь не подходит. Не получается без выдумки —
верни пустой text.
items — конкретные предметы игры, названные в новом text (общие слова — не предметы).
Отвечай строго одним JSON-объектом без пояснений и markdown: {"text": "...", "items": [...]}"""


def persona(prompt_file: Path) -> str:
    text = prompt_file.read_text(encoding="utf-8")
    return text.split(PERSONA_END, 1)[0].strip()


class Soul:
    def __init__(self, store: MemoryStore, complete: Callable[..., dict | None], prompt_file: Path,
                 cfg: SoulConfig | None = None, items=None):
        self.cfg = cfg or SoulConfig()
        self.store = store
        self.complete = complete
        self.persona = persona(prompt_file)
        self.items = items  # companion.game.items.ItemBook — названные предметы сверяем с игрой

    def _unknown(self, items) -> list[str]:
        if not self.items or not isinstance(items, list):
            return []
        return [str(i) for i in items if str(i).strip() and not self.items.exists(str(i))]

    def _checked(self, key: str, text: str, items) -> str:
        """Текст записи, в котором нет выдуманных предметов: как есть, переписанный или пустой (не записывать)."""
        unknown = self._unknown(items)
        if not unknown:
            return text
        similar = list(dict.fromkeys(s for u in unknown for s in self.items.similar(u)))
        user = (f"Запись ({key}): {text}\nТаких предметов в игре нет: {', '.join(unknown)}\n"
                f"Похожие настоящие: {', '.join(similar) or 'нет'}")
        result = self.complete(FIX_SYSTEM, user, title=f"Душа: нет в игре — {', '.join(unknown)}")
        fixed = str(result.get("text") or "").strip() if isinstance(result, dict) else ""
        if fixed and not self._unknown(result.get("items")):
            log.info("Душа: %s — нет в игре %s, переписал: %s", key, unknown, fixed)
            return fixed
        log.info("Душа: %s — нет в игре %s, не записываю: %s", key, unknown, text)
        return ""

    def reflect(self, lines: list[str], where: Where) -> int:
        """Глава записана: что из сказанного Гослингом о себе — в душу. lines — записи главы, как у летописца.
        Возвращает, сколько записей души изменено."""
        if not any("Ты (Гослинг):" in line for line in lines):
            return 0
        current = self.store.soul_feel()
        user = ("Твой характер (описание):\n" + self.persona
                + "\n\nТвоя душа сейчас (key — text (поводы)):\n"
                + ("\n".join(f"- {key} — {text} (поводы: {feel_text(feel)})" for key, text, feel in current)
                   or "пока ничего")
                + "\n\nЗаписи последнего разговора:\n" + "\n".join(lines))
        t = time.perf_counter()
        known = {key: text for key, text, _feel in current}
        known_feel = {key: feel for key, _text, feel in current}
        if self.cfg.screen_model:
            # Дешёвый отбор: четыре главы из пяти записывать нечего — решающую (дорогую) модель зовём, только если
            # отбор предлагает сменить текст души; поводы без смены текста — дело _backfill.
            screen = self.complete(SYSTEM, user, title="Душа: отбор", model=self.cfg.screen_model,
                                   reasoning=self.cfg.screen_reasoning, max_tokens=self.cfg.max_tokens)
            if isinstance(screen, dict) and isinstance(screen.get("soul"), list):
                proposed = [e for e in screen["soul"] if isinstance(e, dict) and str(e.get("key", "")).strip()
                            and self._changes_text(e, known)]
                if not proposed:
                    changed = self._backfill(where)
                    log.info("Душа %.1f с: отбор (%s) — записывать нечего, изменено %d", time.perf_counter() - t,
                             self.cfg.screen_model, changed)
                    return changed
                log.info("Душа: отбор предлагает %s — решает %s", ", ".join(str(e["key"]) for e in proposed),
                         self.cfg.model or "фоновая модель")
            else:
                log.warning("Душа: отбор не ответил — решает основная модель")
        result = self.complete(SYSTEM, user, title="Душа: что я сказал о себе", model=self.cfg.model or None,
                               reasoning=self.cfg.reasoning, max_tokens=self.cfg.max_tokens)
        if not isinstance(result, dict) or not isinstance(result.get("soul"), list):
            log.warning("Душа: модель не вернула ответ")
            return 0
        changed = 0
        for entry in result["soul"]:
            if not isinstance(entry, dict) or not str(entry.get("key", "")).strip():
                continue
            key, text = str(entry["key"]).strip().lower(), str(entry.get("text") or "").strip()
            if text:
                text = self._checked(key, text, entry.get("items"))
                if not text:
                    continue  # выдуманный предмет, переписать не вышло — не записываем (и не стираем прежнее)
            feel = clean_feel(entry.get("feel")) if text else None
            if known.get(key, "") == text and (not feel or known_feel.get(key) is not None):
                continue  # то же самое (поводы у записи уже есть)
            if not text and key not in known:
                continue  # «убрать» то, чего нет
            self.store.set_soul(key, text, str(entry.get("why") or ""), where, feel)
            changed += 1
            log.info("Душа: %s — %s (%s; поводы: %s)", key, text or "убрал", entry.get("why") or "", feel_text(feel))
        changed += self._backfill(where)
        log.info("Душа %.1f с: изменено %d", time.perf_counter() - t, changed)
        return changed

    @staticmethod
    def _changes_text(entry: dict, known: dict[str, str]) -> bool:
        """Запись из ответа меняет текст души: новая, другой текст или «убрать» (пустой text) существующей."""
        key, text = str(entry["key"]).strip().lower(), str(entry.get("text") or "").strip()
        return (text != known[key]) if key in known else bool(text)

    def _backfill(self, where: Where) -> int:
        """Записям души без поводов (сделаны до них) — поводы, одним запросом; нет поводов — пустые, чтобы не
        спрашивать заново."""
        missing = [(key, text) for key, text, feel in self.store.soul_feel() if feel is None]
        if not missing:
            return 0
        # Записи — по номерам: ключ с двоеточием («страх: великаны») модель в ответе обрезала до «страх», и поводов
        # у такой записи не было никогда — спрашивали бы после каждой главы.
        listing = "\n".join(f"{i}. {key} — {text}" for i, (key, text) in enumerate(missing, 1))
        result = self.complete(FEEL_SYSTEM, listing, title=f"Душа: поводы для {len(missing)} записей")
        feels = result.get("feel") if isinstance(result, dict) else None
        if not isinstance(feels, dict):
            log.warning("Душа: поводы не пришли — спросим в следующий раз")
            return 0
        answered = {str(k).strip(". "): v for k, v in feels.items()}
        done = 0
        for i, (key, text) in enumerate(missing, 1):
            if str(i) not in answered:  # нет в ответе — не пишем пустые (они значат «спрашивали, поводов нет»)
                log.warning("Душа: поводов для «%s» в ответе нет — спросим в следующий раз", key)
                continue
            feel = clean_feel(answered[str(i)])
            self.store.set_soul(key, text, "поводы для настроения", where, feel)
            log.info("Душа: поводы %s — %s", key, feel_text(feel))
            done += 1
        return done


def soul_block(store: MemoryStore) -> str:
    """Душа в блок памяти промпта."""
    entries = store.soul()
    if not entries:
        return ""
    return ("Ты сам (что ты о себе решил; о себе говори в согласии с этим, новое о себе решай сам, но не "
            "противоречь):\n" + "\n".join(f"- {key}: {text}" for key, text in entries))
