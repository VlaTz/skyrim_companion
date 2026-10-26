#pragma once

// Приказы Гослингу голосом: жди / следуй. Плагин зовёт скрипт GoslingOrders.Order (Papyrus), тот — ванильный
// квест спутника DialogueFollower, как диалог «Жди здесь» / «Следуй за мной». Движением управляет игра.
namespace orders
{
	// Нативная функция для скрипта GoslingOrders (Done). Регистрируется через SKSE Papyrus interface.
	bool RegisterPapyrus(RE::BSScript::IVirtualMachine* a_vm);

	// Команда сервиса {"type": "order", "action": "wait"|"follow"|"dismiss"|"go_to"|"map", "id": "..."}; у go_to ещё
	// "target" (form маркера карты), "stay" (дойдя, ждать там; false — вернуться к игроку) и "lead" (вести игрока —
	// режим «Веди»); у map — "target": нанести маркер на карту игрока (нашёл Гослинг). Выполняется в игровом
	// потоке, результат — событие order_result: {"id", "ok", "reason"} (reason — код: not_follower, no_path, ...).
	// go_to отвечает, когда Гослинг дошёл или не смог (может пройти не одна минута).
	void HandleCommand(const json& a_command);

	// Загружен сейв: поход (квест похода мода) остановить. Скрипты сохраняются вместе с игрой — поход из сейва шёл
	// дальше и держал Гослинга на месте, хотя сервис о нём уже ничего не знал (21.10: к Поместью Озерному, 1+ мин).
	void StopTravel();
}
