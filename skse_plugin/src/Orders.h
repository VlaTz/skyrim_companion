#pragma once

// Приказы Гослингу голосом: жди / следуй. Плагин зовёт скрипт GoslingOrders.Order (Papyrus), тот — ванильный
// квест спутника DialogueFollower, как диалог «Жди здесь» / «Следуй за мной». Движением управляет игра.
namespace orders
{
	// Нативная функция для скрипта GoslingOrders (Done). Регистрируется через SKSE Papyrus interface.
	bool RegisterPapyrus(RE::BSScript::IVirtualMachine* a_vm);

	// Команда сервиса {"type": "order", "action": "wait"|"follow"|"dismiss"|"go_to", "id": "..."}; у go_to ещё
	// "target" (form маркера карты) и "stay" (дойдя, ждать там; false — вернуться к игроку). Выполняется в игровом
	// потоке, результат — событие order_result: {"id", "ok", "reason"} (reason — код: not_follower, no_path, ...).
	// go_to отвечает, когда Гослинг дошёл или не смог (может пройти не одна минута).
	void HandleCommand(const json& a_command);
}
