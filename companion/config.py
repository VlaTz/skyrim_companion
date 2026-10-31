"""Загрузка config.yaml в типизированные настройки."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class PushToTalkConfig(BaseModel):
    enabled: bool = True
    key: str = "f8"
    min_duration: float = 0.3


class AudioConfig(BaseModel):
    input_device: int | str | None = None
    output_device: int | str | None = None
    sample_rate: int = 16000


class STTConfig(BaseModel):
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    language: str | None = "ru"
    beam_size: int = 5
    vad_filter: bool = True
    initial_prompt: str | None = None


class CloudTTSConfig(BaseModel):
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "elevenlabs/eleven-v3-conversational"
    voice: str = "charlie"     # пусто — голос модели по умолчанию
    api_key: str = ""         # пусто — ключ llm (тот же OpenRouter)
    api_key_env: str = ""
    timeout: float = 30
    char_limit: int = 400     # кусок текста на один запрос
    first_chars: int = 60     # в колонки: первый кусок короче — раньше звучит (в игре — npc_voice.first_chars)
    tags: dict[str, str] = {}  # эмоция -> тег в начале фразы ([happily]); нет в списке — без тега
    references: bool = False   # образец голоса к каждому запросу: characters/<имя>/voice/<эмоция>/ (клон, Fish)


class TTSConfig(BaseModel):
    engine: str = "xtts"      # xtts — локально на видеокарте; cloud — облачный (tts.cloud)
    model: str = "XTTS-v2"
    device: str = "cuda"
    language: str = "ru"
    temperature: float = 0.7
    speed: float = 1.0
    references: bool = True   # xtts: голос и эмоции — по референсам персонажа; False — встроенный голос XTTS (speaker)
    speaker: str = "Viktor Eka"  # xtts без референсов: встроенный диктор из speakers_xtts.pth (58 голосов)
    stream: bool = True
    stream_chunk_size: int = 20
    stream_prebuffer: float = 1.0
    cloud: CloudTTSConfig = CloudTTSConfig()


class PlayerConfig(BaseModel):
    """Игрок: как Гослинг его зовёт — имя уходит во все запросы к моделям и в трассу (companion/naming.py)."""
    name: str = "Владос"
    forms: dict[str, str] = {}   # падежи, если не выводятся сами: gen, dat, ins, pre (Ильи, Илье, Ильёй, Илье)


class LLMConfig(BaseModel):
    base_url: str = "http://localhost:1234/v1"
    model: str = "local-model"
    # Модель для фоновых запросов (летописец: дневник, факты, цели) — их никто не ждёт, а записанное
    # потом долго возвращается в промпт: тут качество важнее скорости. Пусто — та же, что model.
    background_model: str = ""
    background_max_tokens: int = 4000   # с запасом: «размышления» модели тоже считаются в max_tokens
    background_extra_body: dict | None = None  # свои поля запроса для фоновой модели; None — как extra_body
    api_key: str = ""             # сам ключ — только в config.local.yaml (он не в git)
    api_key_env: str = "LLM_API_KEY"  # или имя переменной окружения с ключом
    temperature: float = 0.8
    max_tokens: int = 300
    history_turns: int = 10
    json_mode: bool = False
    timeout: float = 60
    tools: bool = True           # агент может вызывать инструменты-справки (companion/tools.py)
    max_tool_rounds: int = 3     # сколько раз за ответ
    tool_router: bool = True     # планировщик справок: параллельный запрос без истории решает, какие справки нужны
    extra_body: dict = {}  # дополнительные поля запроса, свои у каждого сервера (OpenRouter: reasoning)


class GameConfig(BaseModel):
    # Папка Data игры: оттуда сервис читает строки игры (тексты журнала квестов).
    data_dir: Path = Path(r"D:\Steam\steamapps\common\Skyrim Special Edition\Data")

    @property
    def interface_bsa(self) -> Path:
        return self.data_dir / "Skyrim - Interface.bsa"

    @property
    def lip_generator(self) -> Path:
        # Генератор .lip (губы) из комплекта Creation Kit.
        return self.data_dir.parent / "Tools" / "LipGen" / "LipGenerator" / "LipGenerator.exe"


class NpcVoiceConfig(BaseModel):
    # Голос из уст Гослинга-NPC (companion/voice/npc_voice.py); иначе — колонки.
    enabled: bool = True
    mod_dir: Path = Path(r"D:\Games\Modding\MO2\mods\Gosling")  # мод в MO2 (tools/build_gosling_esp.py)
    max_distance: float = 30   # метров: дальше — Гослинг не говорит (и не слышит), или колонки (speakers)
    max_chars: int = 250       # длина фразы: каждый стык фраз — пауза в игре, поэтому длинные
    first_chars: int = 50      # первая фраза короче: звук начинается, как только она синтезирована целиком
    gap: float = 0.0           # пауза между фразами, с (стык и так даёт паузу: игра принимает Say() не сразу)
    speakers: bool = False     # голосом из игры нельзя (далеко, игра не на связи) — в колонки; False — молчит
    result_timeout: float = 12  # сколько ждать подтверждения от плагина (он ждёт, пока звук пойдёт, до 10 с), с
    lip: bool = True           # губы: .lip к каждой фразе генератором CK (+~0,3 с к первой фразе)

    @property
    def voice_dir(self) -> Path:
        from companion import gosling_mod as mod
        return self.mod_dir / "Sound" / "Voice" / mod.PLUGIN / mod.VOICE_TYPE


class MapConfig(BaseModel):
    known_count: int = 5          # сколько ближайших открытых мест показывать
    unknown_radius: float = 1000  # метров: неоткрытое ближе — видно (без названия)
    unknown_count: int = 3


class MusingsConfig(BaseModel):
    """Случайные размышления о мире в долгой тишине (companion/musings.py)."""
    enabled: bool = True
    gap: float = 600          # секунд — не чаще (от прошлого размышления)
    chance: float = 0.5       # долгая тишина и gap прошёл — с этим шансом размышление вместо обычной реплики
    local: float = 0.5        # шанс взять тему, связанную с местом, где вы сейчас (иначе — любая)
    reasoning: str = "medium"  # рассуждение модели перед репликой: low | medium | high; "" — без него
    max_tokens: int = 4000    # с рассуждением: его токены входят в лимит ответа


class BrainConfig(BaseModel):
    enabled: bool = True
    # Говорить ли самому — решает модель: ставит поводу interest 0–10 (шкала в companion/brain.py) и видит, когда
    # говорила сама; Гослинг говорит, если не ниже interest. Меньше — болтливее, больше — молчаливее.
    interest: int = 7
    # Ниже порога модель пишет короткую шутку к месту; с этим шансом она всё же звучит — не в бою и не чаще
    # раза в quip_gap секунд после прошлой шутки Гослинга (до 2026-10-15 — после любой реплики: шутки не звучали).
    quip_chance: float = 0.2
    quip_gap: float = 90
    ask_gap: float = 2           # секунд между вопросами модели «заговорить ли» (не спрашивать на каждый чих)
    settle: float = 1.5          # подождать, пока соберутся связанные события (вошли в город + помещение)
    repeat_after: float = 600    # одно и то же событие повторно показывать модели не раньше
    idle_after: float = 90       # столько тишины (никто ничего не сказал) — и спросить модель, не заговорить ли
    letter_idle: float = 900     # Гослинг далеко: столько секунд с расставания или прошлого письма — записка Владосу
    # Пока Гослинг говорит или отвечает, поводы ждут; старше этого — уже не про то, что сейчас (не спрашиваем).
    fresh_for: float = 15        # секунд — обычный повод (вошли в помещение, сундук)
    fresh_important: float = 45  # секунд — важный (задание, знакомый, впервые увиденное, Крик, тяжело ранен)
    # Какие события показывать модели (остальные — только в «Недавно»: убитые враги, «заговорил с NPC»).
    events: list[str] = [
        "loaded", "location", "wilderness", "interior", "combat_started", "combat_ended", "low_health", "item",
        "discovered", "profile", "power", "person", "quest", "quest_misc", "cleared", "crime", "talk", "deed",
        "activated", "unlocked", "reading", "looted", "furniture", "novel", "threat", "thought", "died", "gift", "taken",
        "hurt_self", "personal", "order_failed", "rejoined", "arrived", "own_fight", "own_fight_end", "own_unknown",
        "talked", "oddity",
    ]
    # Подслушанное (реплики NPC рядом): решаем, когда разговор стих и игрок вышел из меню разговора.
    overheard_quiet: float = 3   # секунд без новых реплик — разговор закончился
    musings: MusingsConfig = MusingsConfig()


class DiaristConfig(BaseModel):
    """Летописец (companion/memory/diarist.py): когда глава кончилась и пора писать дневник, факты, цели."""
    min_entries: int = 6         # записей журнала, чтобы глава вообще стоила записи (и при уходе из места)
    big_dialogue: int = 20       # реплик игрока и Гослинга — пора записать, даже если никуда не ушли
    big_total: int = 60          # записей всего (в городе подслушанное копится быстро)
    chapter_seconds: float = 900  # секунд с первой неописанной записи — пора записать
    check_every: float = 30      # секунд — как часто проверять
    max_heard: int = 20          # подслушанного в запрос — не больше (последнее)
    max_lines: int = 120         # всего строк журнала в запрос
    known_facts: int = 40        # уже известных фактов в запрос (чтобы не повторялся)
    model: str = ""              # своя модель летописца (и сверки фактов); пусто — llm.background_model
    reasoning: str = ""          # рассуждение: low | medium | high; "" — как у остальных фоновых
    max_tokens: int = 0          # лимит ответа вместе с рассуждением; 0 — llm.background_max_tokens


class MoodConfig(BaseModel):
    """Настроение Гослинга (companion/mood.py): из событий игры, само затухает."""
    enabled: bool = True
    voice_default: bool = True  # модель ответила нейтрально, а настроение сильное — голос с эмоцией настроения


class VisionConfig(BaseModel):
    """Зрение Гослинга (companion/vision.py): кадр игры — модели с картинками, описание — в обстановку и look."""
    enabled: bool = True
    model: str = "anthropic/claude-haiku-5.5"  # видит картинки; пусто — llm.model
    max_distance: float = 25    # м — Гослинг дальше (или не загружен) — не видит, что видно Владосу
    fresh: float = 120          # с — описание вида держится в обстановке (на том же месте)
    glance_on: list[str] = ["location", "wilderness", "discovered", "weather"]  # события — взглянуть самому
    silence: float = 45         # с без реплик — тишина: взглянуть, если давно не смотрел
    silence_gap: float = 180    # с — в тишине не чаще
    after_load: float = 10      # с после загрузки сейва — не смотреть (на экране ещё загрузка)
    revisit: float = 30         # с — вернулись туда, откуда ушли раньше этого, — заново не смотреть
    settle: float = 2           # с — подождать перед взглядом сам (событие приходит, пока на экране загрузка)
    width: int = 1024           # px — кадр уменьшается до этой ширины
    quality: int = 80           # JPEG
    frames: int = 20            # последних кадров хранить в logs/frames (для разбора); 0 — не хранить
    max_tokens: int = 400
    extra_body: dict | None = None  # свои поля запроса; None — как llm.extra_body (Sonnet: {reasoning: {effort: low}})


class PersonalConfig(BaseModel):
    """Личные реплики (companion/personal.py): событие задело запись души — повод заговорить от себя."""
    enabled: bool = True
    gap: float = 900      # с — одну запись души не чаще
    gap_all: float = 120  # с — любую не чаще


class MysteriesConfig(BaseModel):
    """Загадки (companion/mysteries.py): «швы» игры, которые Гослинг видит изнутри мира, — удивляется, гадает, спрашивает."""
    enabled: bool = True
    gap: float = 7200            # с — неразгаданную загадку после догадки поднимать не чаще (в остальное время — молча)
    time_jump_hours: float = 2   # ч игрового времени сверх обычного хода — «провал во времени» (сон, ожидание, перемещение)
    repeat_lines: int = 3        # раз слово в слово одну реплику NPC (в разное время) — «одинаковые слова»
    bag_items: int = 60          # вещей в сумке Владоса — «столько не унести»
    bag_more: int = 40           # и снова — когда прибавилось ещё столько


class SoulConfig(BaseModel):
    """Душа Гослинга (companion/memory/soul.py): после главы летописца — что из сказанного о себе запомнить."""
    enabled: bool = True
    model: str = ""            # решающая модель души; пусто — llm.background_model
    reasoning: str = "medium"  # её рассуждение: low | medium | high; "" — как у остальных фоновых
    max_tokens: int = 6000     # с рассуждением: его токены входят в лимит
    # Дешёвый отбор: после каждой главы сперва он; нечего записывать (так в 4 главах из 5) — model не зовём.
    screen_model: str = ""     # пусто — без отбора, сразу model
    screen_reasoning: str = "medium"


class ExplorerConfig(BaseModel):
    """Исследователь (companion/explorer.py): «отпусти исследовать» и «веди» — Гослинг сам решает, куда идти."""
    enabled: bool = True
    model: str = ""              # модель решений «куда дальше»; пусто — llm.background_model
    radius: float = 2000         # м — один: места не дальше от Гослинга
    lead_radius: float = 700     # м — «веди»: Владос идёт пешком рядом, места поближе
    candidates: int = 8          # мест на выбор модели — ближайших
    max_places: int = 4          # мест за вылазку — потом домой (к Владосу)
    max_minutes: float = 20      # минут вылазки — потом к Владосу (уйдёт и с концами — нет)
    town_places: int = 20        # в поселении — целей больше: там очень много всего (здания, улицы)
    town_minutes: float = 60     # и минут
    remember_minutes: float = 60  # минут «Последняя вылазка» видна в разговоре — итоги, когда вы снова вместе
    reasoning: str = "low"       # рассуждение перед решением «куда дальше»: low | medium | high; "" — без
    alone_reasoning: str = "medium"  # то же, когда он один («исследуй»): Владос не ждёт ответа — можно думать дольше
    max_tokens: int = 0          # лимит ответа вместе с рассуждением; 0 — llm.background_max_tokens
    lead_pause: float = 8        # с — «Веди»: дошли, Владос подошёл — столько осмотреться, потом дальше (20 — долго)
    look_around: float = 8       # с — дошёл (Владос рядом): осмотреться и послушать, кто о чём говорит, потом решать
    listen: float = 10           # с — подошёл к человеку: послушать, что он говорит (дальше — заговорить)
    linger: float = 90           # с — «побыть здесь»: постоять, посидеть, послушать разговоры вокруг (таверна, площадь)


class TalkConfig(BaseModel):
    """Разговоры Гослинга с NPC (companion/talk.py): ответ NPC — модель в его роли по данным игры, голос — его."""
    enabled: bool = True
    model: str = ""              # модель реплик (NPC и Гослинга в разговоре); пусто — explorer.model
    turns: int = 3               # обменов репликами за разговор (Гослинг — NPC), не больше
    distance: float = 6          # м от Гослинга до NPC — дальше заговорить нельзя (не услышит; сначала подойти)
    voices_bsa: str = "Skyrim - Voices_ru0.bsa"  # реплики игры — образцы голосов NPC
    refs: int = 3                # образцов голоса на тип голоса
    quotes: int = 8              # его реплик из игры — в роль (как он говорит и что знает)


class ThinkerConfig(BaseModel):
    """Мысли Гослинга (companion/memory/thinker.py): что держать в голове и предложить Владосу."""
    enabled: bool = True
    min_gap: float = 120         # секунд между раздумьями (вошли в город и сразу в лавку — одно раздумье)
    quiet_every: float = 600     # секунд — подумать и без перемен
    bag_change: int = 3          # вещей в сумке прибавилось или убыло — повод подумать
    check_every: float = 3       # секунд — как часто смотреть, не поменялось ли что (проверка дешёвая)
    max_ideas: int = 3           # мыслей за раз — не больше
    dialogue_lines: int = 12     # последних реплик в запрос — что уже предлагал и что ответил Владос
    revisit: float = 30          # с — вернулись туда, откуда ушли раньше этого, — заново не думать (граница локации)
    model: str = ""              # своя модель мыслителя; пусто — llm.background_model
    reasoning: str = ""          # её рассуждение: low | medium | high; "" — как у остальных фоновых
    max_tokens: int = 0          # лимит ответа вместе с рассуждением; 0 — llm.background_max_tokens


class JokesConfig(BaseModel):
    """Шутки Гослинга (companion/jokes.py): заготовки из characters/<персонаж>/jokes.yaml к обстановке."""
    enabled: bool = True
    refill: bool = True          # дописывать новые в фоне (jokes_auto.yaml), когда к обстановке мало несказанных
    refill_below: int = 2        # несказанных заготовок к обстановке меньше — дописать
    refill_count: int = 6        # сколько дописать за раз
    refill_gap: float = 900      # секунд между дописываниями — не чаще (каждое — запрос к сильной модели)
    model: str = "anthropic/claude-sonnet-5.5"  # кто пишет: почти как Opus, вдвое дешевле


class OverlayConfig(BaseModel):
    """Окно с логом поверх игры (companion/overlay.py). Видно в окне без рамки, не в эксклюзивном полном экране."""
    enabled: bool = True
    key: str = "f10"             # показать / спрятать (имя клавиши pynput, как push_to_talk.key)
    visible: bool = False        # показано сразу при запуске
    corner: str = "top-right"    # top-right | top-left | bottom-right | bottom-left
    width: int = 760
    height: int = 380
    margin: int = 20             # отступ от края экрана, пикселей
    alpha: float = 0.8           # непрозрачность 0..1
    font_size: int = 10
    capture: bool = False        # видно в записи экрана (OBS); от снимков зрения прячется только на их время
    lines: int = 300             # сколько последних строк держать


class Config(BaseModel):
    models_dir: Path = Path("models")
    memory_dir: Path = Path("memory")  # память компаньона (SQLite); в git не идёт — это твоё прохождение
    logs_dir: Path = Path("logs")      # логи; трасса Гослинга — logs/trace/<дата>.log
    trace: bool = True                 # писать трассу: вопросы, справки и ответы модели, озвучка, летописец
    character: str = "gosling"
    player: PlayerConfig = PlayerConfig()
    server: ServerConfig = ServerConfig()
    push_to_talk: PushToTalkConfig = PushToTalkConfig()
    echo: bool = True
    audio: AudioConfig = AudioConfig()
    stt: STTConfig = STTConfig()
    tts: TTSConfig = TTSConfig()
    llm: LLMConfig = LLMConfig()
    game: GameConfig = GameConfig()
    npc_voice: NpcVoiceConfig = NpcVoiceConfig()
    map: MapConfig = MapConfig()
    brain: BrainConfig = BrainConfig()
    diarist: DiaristConfig = DiaristConfig()
    thinker: ThinkerConfig = ThinkerConfig()
    explorer: ExplorerConfig = ExplorerConfig()
    talk: TalkConfig = TalkConfig()
    soul: SoulConfig = SoulConfig()
    mood: MoodConfig = MoodConfig()
    personal: PersonalConfig = PersonalConfig()
    mysteries: MysteriesConfig = MysteriesConfig()
    vision: VisionConfig = VisionConfig()
    jokes: JokesConfig = JokesConfig()
    overlay: OverlayConfig = OverlayConfig()

    @property
    def models_path(self) -> Path:
        return self.models_dir if self.models_dir.is_absolute() else ROOT / self.models_dir

    @property
    def character_path(self) -> Path:
        return ROOT / "characters" / self.character

    @property
    def logs_path(self) -> Path:
        return self.logs_dir if self.logs_dir.is_absolute() else ROOT / self.logs_dir

    @property
    def memory_path(self) -> Path:
        """Память компаньона: файл на каждого персонажа игрока (companion/memory)."""
        root = self.memory_dir if self.memory_dir.is_absolute() else ROOT / self.memory_dir
        return root / self.character


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def load_config(path: Path | None = None) -> Config:
    """config.yaml + config.local.yaml поверх него (секреты и личные настройки, не в git)."""
    path = path or ROOT / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    local = path.with_name(path.stem + ".local.yaml")
    if local.is_file():
        data = _merge(data or {}, yaml.safe_load(local.read_text(encoding="utf-8")) or {})
    cfg = Config.model_validate(data or {})
    from companion import naming  # имя Игрока — во все запросы к моделям и в трассу (сервер и инструменты разом)
    naming.setup(cfg.player.name, cfg.player.forms)
    return cfg
