#pragma once

// Голос из уст Гослинга (NPC из Gosling.esp): сервис кладёт .wav реплики-заглушки, плагин
// заставляет Гослинга её произнести через Papyrus (GoslingVoice.SayLine -> Actor.Say).
namespace voice
{
	// Нативные функции для скрипта GoslingVoice (Log). Регистрируется через SKSE Papyrus interface.
	bool RegisterPapyrus(RE::BSScript::IVirtualMachine* a_vm);

	// В лог: запущен ли квест-контейнер тем голоса (без него игра молча не произносит реплики).
	void LogQuestState();

	// Гослинг в мире (постоянная ссылка из Gosling.esp); nullptr — мод не подключён.
	RE::Actor* Gosling();

	// Это Гослинг (его собственные реплики сервису пересказывать не нужно).
	bool IsGosling(const RE::TESObjectREFR* a_ref);

	// Где Гослинг: {"loaded", "distance" (м), "follower", "waiting", ...}; null — Gosling.esp не подключён.
	json Status(RE::PlayerCharacter* a_player);

	// Команда сервиса {"type": "say", "line": 0|1, "id": "...", "text": "фраза для субтитра"} — выполняется в игровом потоке,
	// результат уходит событием say_result: ok — когда звук реплики реально пошёл (до 10 с после команды).
	void HandleCommand(const json& a_command);
}
