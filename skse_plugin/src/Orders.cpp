#include "Orders.h"

#include "Net.h"
#include "Voice.h"

namespace orders
{
	namespace
	{
		void Report(const std::string& a_id, const std::string& a_reason)
		{
			if (a_reason.empty()) {
				logs::info("Приказ {} выполнен", a_id);
			} else {
				logs::warn("Приказ {} не выполнен: {}", a_id, a_reason);
			}
			net::Post({ { "type", "order_result" },
				{ "data", { { "id", a_id }, { "ok", a_reason.empty() }, { "reason", a_reason } } } });
		}

		// GoslingOrders.Done(id, reason) — скрипт закончил приказ (reason пусто — выполнен).
		void PapyrusDone(RE::StaticFunctionTag*, std::string a_id, std::string a_reason)
		{
			Report(a_id, a_reason);
		}
	}

	bool RegisterPapyrus(RE::BSScript::IVirtualMachine* a_vm)
	{
		a_vm->RegisterFunction("Done", "GoslingOrders", PapyrusDone);
		return true;
	}

	void HandleCommand(const json& a_command)
	{
		auto id = a_command.value("id", "");
		auto action = a_command.value("action", "");
		logs::info("Приказ {} ({}) получен", id, action);
		// В отличие от реплики, приказ не устаревает за секунды: игра стояла на паузе — выполним после.
		const auto target = a_command.value("target", RE::FormID{ 0 });  // go_to: маркер карты
		const bool stay = a_command.value("stay", true);                  // go_to: дойдя, ждать там (или вернуться)
		SKSE::GetTaskInterface()->AddTask([id = std::move(id), action = std::move(action), target, stay] {
			const auto actor = voice::Gosling();
			if (!actor) {
				Report(id, "no_gosling");
				return;
			}
			const auto vm = RE::BSScript::Internal::VirtualMachine::GetSingleton();
			RE::BSTSmartPointer<RE::BSScript::IStackCallbackFunctor> callback;
			bool dispatched = false;
			if (action == "go_to") {
				const auto ref = RE::TESForm::LookupByID<RE::TESObjectREFR>(target);
				if (!ref) {
					Report(id, "no_target");
					return;
				}
				logs::info("Приказ {}: идти к {:08X}, {} м от Гослинга, потом {}", id, target,
					actor->GetPosition().GetDistance(ref->GetPosition()) / 70.0f, stay ? "ждать там" : "вернуться");
				const auto args = RE::MakeFunctionArguments(static_cast<RE::Actor*>(actor), static_cast<RE::TESObjectREFR*>(ref),
					bool(stay), std::string(id));
				dispatched = vm && vm->DispatchStaticCall("GoslingOrders", "GoTo", args, callback);
			} else {
				const auto args = RE::MakeFunctionArguments(static_cast<RE::Actor*>(actor), std::string(action), std::string(id));
				dispatched = vm && vm->DispatchStaticCall("GoslingOrders", "Order", args, callback);
			}
			if (!dispatched) {
				Report(id, "papyrus");
			}
		});
	}
}
