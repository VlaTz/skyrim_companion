"""Устройство мода Gosling.esp — общее для сборщика (tools/build_gosling_esp.py) и сервиса.

Номера форм — локальные (без индекса мода в списке загрузки): в игре плагин ищет их через
TESDataHandler::LookupForm(номер, "Gosling.esp"). В C++ они продублированы в skse_plugin/src/Voice.cpp.
"""

PLUGIN = "Gosling.esp"
# Свой тип голоса для реплик сервиса (как MantellaVoice00 у Mantella): на время Say() Гослингу ставится
# он, файлы лежат в его папке. Обычный голос Гослинга (бой, «веди») — MaleYoungEager Эрика, шаблоном.
VOICE_TYPE = "GoslingVoice"
VOICE_TYPE_ID = 0x814

NPC = 0x800          # GoslingNPC — базовая запись
RELATIONSHIP = 0x801
NPC_REF = 0x802      # сам Гослинг в мире (постоянная ссылка)
VOICE_QUEST = 0x803  # квест-контейнер для тем-заглушек

QUEST_EDID = "GoslingVQ"
# Две темы-заглушки по одной реплике: (тема, реплика, EditorID темы). Чередуем их: если сказать одну
# тему дважды подряд, игра может проиграть старый звук из кэша.
LINES = [(0x810, 0x811, "GoslingLine1"), (0x812, 0x813, "GoslingLine2")]
NEXT_OBJECT_ID = 0x815


def voice_file(line: int) -> str:
    """Имя .wav реплики: <квест>_<тема>_<formID реплики без индекса мода>_<номер ответа>.
    EditorID короткие (≤10 и ≤15 символов) — игра их не обрезает."""
    _, info, topic_edid = LINES[line]
    return f"{QUEST_EDID}_{topic_edid}_{info:08X}_1.wav"
