# Гослинг — ИИ-компаньон для Skyrim SE

Гослинг — спутник в Skyrim Special Edition, которым управляет LLM-агент. Он говорит своим голосом из уст NPC,
слышит вас (говорите в микрофон) и NPC вокруг, сам заговаривает, помнит прохождение, знает мир из файлов игры,
выполняет просьбы («жди», «пошли», «сходи в Ривервуд», «поговори с ним»), сам исследует места и разговаривает с
жителями, удивляется странностям мира.

Устройство — [docs/Гослинг_схема.html](docs/Гослинг_схема.html) (откройте в браузере), журнал разработки —
[production_logs/](production_logs/).

> **Сейчас работает только с русской версией игры** (тексты и озвучка). Только Windows.

---

## Что понадобится

| Что | Зачем | Где взять |
|---|---|---|
| **Windows 10/11** | игра, SKSE, сервис | — |
| **Skyrim Special Edition (Steam), русская версия** | сама игра; проверено на версии **1.7.104** | Steam |
| **Creation Kit** (бесплатно) | генератор движения губ (`.lip`) — сервис делает его к каждой фразе Гослинга во время игры; компилятор скриптов мода | Steam (шаг 1) |
| **SKSE64** под вашу версию игры | загрузчик плагинов (для 1.7.104 — SKSE 2.3.1) | [skse.silverlock.org](https://skse.silverlock.org) |
| **Mod Organizer 2** | менеджер модов | [Nexus](https://www.nexusmods.com/skyrimspecialedition/mods/6194) или [GitHub](https://github.com/ModOrganizer2/modorganizer/releases) |
| **Address Library for SKSE Plugins** под вашу версию игры | нужна плагину Гослинга | [Nexus](https://www.nexusmods.com/skyrimspecialedition/mods/32444) |
| **Python 3.11** | сервис Гослинга | [python.org](https://www.python.org/downloads/) (при установке — «Add to PATH») |
| **Git** | скачать проект | [git-scm.com](https://git-scm.com/) |
| **Видеокарта NVIDIA** (рекомендуется) | распознавание речи и голос локально; проверено на RTX 50xx | без неё — облачный голос и Whisper на процессоре (медленнее) |
| **Ключ OpenRouter** (платно за запросы) | модели: ответы Гослинга, память, зрение | [openrouter.ai/keys](https://openrouter.ai/keys) |
| **Микрофон** | говорить с Гослингом (клавиша F8) | — |
| ~10 ГБ на диске | модели распознавания и голоса | — |

---

## Что лежит в GitHub, а что собирается у вас

В репозитории — только код и тексты: сервис (`companion/`), исходники SKSE-плагина (`skse_plugin/`) и скриптов мода
(`mod/Scripts/Source/`) с уже скомпилированными скриптами (`mod/Scripts/*.pex`), инструменты (`tools/`), характер и
шутки Гослинга (`characters/gosling/`).

**Мод Gosling, голос Гослинга и модели в репозитории не лежат — и класть их туда не нужно:**
- **мод `Gosling.esp`** собран из записей `Skyrim.esm` (NPC по образцу Эрика, пакеты ИИ), лицо NPC — из архивов игры.
  Это файлы Bethesda, распространять их нельзя; скрипт `tools/build_gosling_esp.py` собирает мод из **вашей** копии игры;
- **голос Гослинга** (референсы для XTTS) и образцы голосов NPC — реплики из архивов игры; вытаскиваются у вас скриптами;
- **модели** Whisper и XTTS — скачиваются скриптом с Hugging Face (у XTTS — некоммерческая лицензия);
- **SKSE-плагин** `SkyrimCompanion.dll` — свой код (GPL-3.0): готовая сборка — в
  [Releases](https://github.com/VlaTz/skyrim_companion/releases); можно собрать и самому (шаг 7).

Память Гослинга (`memory/`), логи (`logs/`) и ключ (`config.local.yaml`) — ваши личные, в git не попадают.

---

## Установка

Пути ниже — для примера: игра в `C:\Program Files (x86)\Steam\steamapps\common\Skyrim Special Edition`, MO2 в
`C:\Modding\MO2`, проект в `C:\skyrim_companion`. Подставьте свои.

### 1. Игра на русском и Creation Kit

1. Steam → Skyrim Special Edition → **Свойства → Язык → Русский**. Запустите игру один раз и закройте.
2. Отключите автообновление игры (Steam → Свойства → Обновления → «Обновлять только при запуске»): после обновления игры
   SKSE и Address Library перестают подходить, пока не выйдут их новые версии.
3. **Creation Kit** — официальный редактор Bethesda, бесплатный для владельцев игры. Нужен Гослингу ради двух
   инструментов из его комплекта: генератора движения губ (`.lip`; фразы Гослинга рождаются во время игры, и движение
   губ к каждой сервис делает тут же) и компилятора скриптов мода.
   - Steam → Магазин → найдите **«Skyrim Special Edition: Creation Kit»** → «Играть»/«Установить» (если не находится —
     Библиотека → в списке слева включите показ «Инструменты»). Он ставится в папку игры, отдельной папки не нужно.
   - Запустите Creation Kit из Steam **один раз** (Steam должен быть запущен). Если спросит, распаковать ли скрипты
     (`Scripts.zip`), — «Да». Дождитесь главного окна редактора и закройте его; ничего открывать и сохранять не нужно.
   - Проверьте папку игры: должны быть `Tools\LipGen\LipGenerator\LipGenerator.exe`, `Papyrus Compiler\PapyrusCompiler.exe`
     и `Data\Source\Scripts\` (тысячи `.psc`). Если `Data\Source\Scripts\` нет — распакуйте `Data\Scripts.zip` в `Data\`
     (архиватором; внутри уже есть папка `Source\Scripts`).

### 2. SKSE64

1. Скачайте с [skse.silverlock.org](https://skse.silverlock.org) сборку **под вашу версию игры** (версия — в свойствах
   файла `SkyrimSE.exe`; для 1.7.104 — SKSE 2.3.1).
2. Из архива скопируйте `skse64_loader.exe` и `skse64_*.dll` **в папку игры** (рядом с `SkyrimSE.exe`).
3. Папку `Data` из архива (в ней `Scripts`) не копируйте в игру — она станет модом в MO2 (шаг 3).

### 3. Mod Organizer 2

1. Установите MO2, при первом запуске создайте **портативный экземпляр** (Portable) в отдельной папке, например
   `C:\Modding\MO2`, и укажите игру **Skyrim Special Edition** (папку Steam).
2. Моды → «Установить мод» (значок архива) — поставьте два мода:
   - **SKSE Scripts** — тот же архив SKSE: MO2 предложит взять из него папку `Data` (скрипты SKSE);
   - **Address Library for SKSE Plugins** — файл для вашей версии игры (для 1.6+ — «All in one (Anniversary
     Edition)»); внутри должен оказаться `SKSE\Plugins\versionlib-1-7-104-0.bin` (номер — ваша версия игры).
3. Включите оба мода галочками слева.
4. Сверху справа, в списке запуска, выберите **SKSE** (MO2 находит `skse64_loader.exe` сам). Запускать игру теперь —
   только так: через MO2 → SKSE.
5. Проверка: запустите игру через SKSE, откройте консоль (`ё` / `~`) и введите `getskseversion` — должна ответить
   версия SKSE. Закройте игру.

### 4. Проект и Python

```bash
git clone https://github.com/VlaTz/skyrim_companion.git
```

```bash
cd skyrim_companion
```

```bash
py -3.11 -m venv .venv
```

Torch — отдельно, под вашу видеокарту (команда для CUDA 12.8 — RTX 50xx и новее; для старых карт — свою с
[pytorch.org](https://pytorch.org/get-started/locally/)):

```bash
.venv\Scripts\python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
```

```bash
.venv\Scripts\python -m pip install -r requirements.txt
```

### 5. Личный конфиг

Скопируйте `config.local.example.yaml` в **`config.local.yaml`** и впишите:
- `llm.api_key` — ключ OpenRouter;
- `player.name` — как Гослинг будет вас звать;
- `game.data_dir` — папка `Data` игры;
- `npc_voice.mod_dir` — папка, где будет мод Gosling в MO2: `C:\Modding\MO2\mods\Gosling` (её создаст шаг 8);
- без видеокарты NVIDIA: `stt.model: small`, `stt.device: cpu`, `stt.compute_type: int8`, `tts.engine: cloud`.

`config.local.yaml` перекрывает `config.yaml` и в git не попадает; остальные настройки (модели, частота реплик,
исследователь, загадки) — в `config.yaml`, у каждой — комментарий.

### 6. Модели и данные из игры

Модели распознавания речи и голоса (~3,5 ГБ):

```bash
.venv\Scripts\python tools/download_models.py --only whisper:large-v3-turbo --only xtts
```

Без видеокарты — `--only whisper:small` (и `xtts` не нужен, если `tts.engine: cloud`).

Голос Гослинга для XTTS — реплики Эрика (тип голоса `maleyoungeager`) из архива игры, по эмоциям:

```bash
.venv\Scripts\python tools/extract_voices.py maleyoungeager
```

```bash
.venv\Scripts\python tools/extract_dialogue.py maleyoungeager
```

```bash
.venv\Scripts\python tools/make_voice_refs.py --voice maleyoungeager --character gosling
```

Реплики NPC из игры — по ним NPC отвечают Гослингу своей манерой и знанием:

```bash
.venv\Scripts\python tools/extract_quotes.py
```

Образцы голосов NPC для разговоров собираются сами при первом разговоре (`data/npc_voices/`).

Проверка голоса и распознавания без игры (синтез → распознавание по кругу):

```bash
.venv\Scripts\python tools/selftest.py --play
```

### 7. SKSE-плагин

**Готовый:** скачайте архив `SkyrimCompanion-v….zip` из
[Releases](https://github.com/VlaTz/skyrim_companion/releases) (не распаковывайте). В MO2: «Установить мод» (значок
архива) → выберите архив → имя **Skyrim Companion** → OK. Включите мод галочкой. Внутри мода должно оказаться
`SKSE\Plugins\SkyrimCompanion.dll`.

**Собрать самому** (нужны Visual Studio 2022 или Build Tools 2022 с «Разработкой классических приложений на C++» и
[xmake](https://xmake.io) 3.0+):

```bash
git submodule update --init --recursive
```

```bash
cd skse_plugin
```

```bash
xmake f --skyrim_vr=n -m releasedbg -y
```

```bash
xmake build -y
```

Результат — `skse_plugin\build\windows\x64\releasedbg\SkyrimCompanion.dll`. В MO2: Моды → правый клик → «Создать пустой
мод» → **Skyrim Companion**; правый клик по нему → «Открыть в проводнике»; положите DLL в `SKSE\Plugins\` внутри мода.
Включите мод.

### 8. Мод Gosling

Собирается из вашей копии игры инструментами Creation Kit (шаг 1) и сам кладётся в папку `npc_voice.mod_dir`:

```bash
.venv\Scripts\python tools/build_gosling_esp.py --install
```

В выводе не должно быть «губы шевелиться не будут» — если есть, Creation Kit не установлен или не запускался (шаг 1).

В MO2: **F5** (обновить), включите мод **Gosling**, справа на вкладке **Plugins** включите **Gosling.esp**.

После обновления проекта (`git pull`) пересобирайте мод этой же командой, если менялись `mod/`, `tools/build_gosling_esp.py`
или `companion/gosling_mod.py`; плагин — если менялся `skse_plugin/src/`.

---

## Запуск

1. **Сервис** — двойной клик по `start.bat` (или `.venv\Scripts\python -m companion`). Дождитесь строки
   «Сервис готов: http://127.0.0.1:8765». Первый запуск дольше — загрузка моделей.
2. **Игра** — MO2 → **SKSE** → «Запустить».
3. **Наймите Гослинга** — см. ниже.
4. **Говорите**: держите **F8**, скажите фразу, отпустите. Гослинг ответит своим голосом.
   **F10** — окно с логом поверх игры (видно, что он «думает»).

### Где нанять Гослинга

Гослинг ждёт в **таверне «Мороженый фрукт» в Рорикстеде**, рядом с местом наёмника Эрика (Гослинг на него похож).
Рорикстед — деревня к западу от Вайтрана, по дороге на Маркарт: от Вайтрана пешком или верхом на запад.

1. Если с вами уже есть спутник, отпустите его: заговорите с ним → «Пора нам расстаться.» Спутник может быть только
   один.
2. Заговорите с Гослингом → **«Следуй за мной. Мне нужна твоя помощь.»** Платить не нужно, условий нет.
3. Готово: он идёт за вами, а дальше управляется голосом. Гослинг бессмертен (в бою падает на колено и встаёт).

Быстро попасть в таверну (например, на новой игре, чтобы проверить установку) — консолью (`ё` / `~`):
`coc RoriksteadFrostfruitInn`.

Попробуйте: «Как тебя зовут?», «Что видишь?», «Кто такие Братья Бури?», «Подожди меня здесь», «Пошли за мной»,
«Исследуй город», «Поговори со стражником, спроси, что нового».

---

## Если что-то не работает

| Признак | Что проверить |
|---|---|
| Гослинга нет в таверне / нет его голоса | мод **Gosling** и **Gosling.esp** включены в MO2 (F5 после сборки); игра запущена через **SKSE** |
| Гослинг молчит, в логе сервиса нет событий игры | плагин не загрузился: мод **Skyrim Companion** включён, **Address Library** под вашу версию игры; лог плагина — `Документы\My Games\Skyrim Special Edition\SKSE\SkyrimCompanion.log` |
| `getskseversion` не отвечает | игра запущена не через «SKSE» в MO2, или версия SKSE не под вашу игру |
| В логе сервиса `401` | ключ в `config.local.yaml` (`llm.api_key`) — без пробелов и лишних строк |
| «Нет модели Whisper» / «Нет референсов» | шаг 6: скачать модели под `stt.model`; собрать голос (`make_voice_refs.py`) — или `tts.references: false` |
| Губы Гослинга не шевелятся | есть ли `Tools\LipGen\LipGenerator\LipGenerator.exe` в папке игры (Creation Kit, шаг 1); после установки — пересобрать мод (шаг 8) и перезапустить сервис |
| Ответы очень медленные | `stt.device: cuda`, `tts.device: cuda`; или `tts.engine: cloud` |

Всё, что Гослинг видел, решал и говорил, — в трассе `logs/trace/<дата>.log`.

### Проверка без игры

Стенд собирает весь сервис в одном процессе (подменены только звук, микрофон и плагин) и прогоняет сценарии на
настоящих моделях — разговор, зрение, исследователь, NPC, память, загадки (около 15 минут, платно за запросы):

```bash
.venv\Scripts\python tools/eval_gosling.py
```

Результат — `logs/eval/<время>/transcript.md`.

---

## Ограничения

- Только русская версия игры (строки, голоса `Voices_ru0.bsa`) и только Windows.
- Гослинг — обычный спутник игры: одновременно с ним другого спутника взять нельзя.
- Вдали от игрока игра мир не просчитывает: исследовать здания и говорить с людьми он может, только когда вы рядом.
- Голос XTTS-v2 — некоммерческая лицензия Coqui; облачный голос и модели — платно через OpenRouter.

## Лицензии

SKSE-плагин — GPL-3.0 (как CommonLibSSE-NG). Skyrim, его файлы, голоса и персонажи принадлежат Bethesda; проект —
фанатский, не связан с Bethesda.
