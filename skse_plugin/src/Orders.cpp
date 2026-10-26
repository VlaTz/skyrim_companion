#include "Orders.h"

#include "Events.h"
#include "Net.h"
#include "Voice.h"

namespace orders
{
	namespace
	{
		// take из сундука: id приказа -> сундук. Когда скрипт закончит — что в сундуке осталось (по заданию: Гослинг
		// такое не берёт, Владос возьмёт сам). Только в игровом потоке.
		std::map<std::string, RE::FormID> takes;

		void Report(const std::string& a_id, const std::string& a_reason, json a_extra = json::object())
		{
			if (a_reason.empty()) {
				logs::info("Приказ {} выполнен", a_id);
			} else {
				logs::warn("Приказ {} не выполнен: {}", a_id, a_reason);
			}
			json data = { { "id", a_id }, { "ok", a_reason.empty() }, { "reason", a_reason } };
			data.update(a_extra);
			net::Post({ { "type", "order_result" }, { "data", std::move(data) } });
		}

		json LeftIn(RE::TESObjectREFR* a_container)
		{
			json left = json::array();
			for (const auto& [object, data] : a_container->GetInventory([](RE::TESBoundObject&) { return true; }, true)) {
				if (auto name = net::ToUtf8(object ? object->GetName() : ""); data.first > 0 && !name.empty()) {
					left.push_back(data.first > 1 ? std::format("{} ×{}", name, data.first) : name);
				}
			}
			return left;
		}

		// GoslingOrders.Done(id, reason) — скрипт закончил приказ (reason пусто — выполнен).
		void PapyrusDone(RE::StaticFunctionTag*, std::string a_id, std::string a_reason)
		{
			json extra = json::object();
			if (const auto it = takes.find(a_id); it != takes.end()) {
				if (const auto chest = RE::TESForm::LookupByID<RE::TESObjectREFR>(it->second)) {
					extra["left"] = LeftIn(chest);
				}
				takes.erase(it);
			}
			Report(a_id, a_reason, std::move(extra));
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
		const auto target = a_command.value("target", RE::FormID{ 0 });  // go_to, map, take: ссылка (номер в игре)
		// go_to внутрь подземелья: ссылка из файлов игры (особая точка локации) — плагин и номер без индекса мода
		auto file = a_command.value("file", "");
		const auto local = a_command.value("local", RE::FormID{ 0 });
		const bool stay = a_command.value("stay", true);    // go_to: дойдя, ждать там (или вернуться)
		const bool lead = a_command.value("lead", false);   // go_to: вести игрока (ждать, если отстал)
		// go_to: дошёл, когда ближе этого (м); 0 — по умолчанию скрипта (~20 м: маркер карты бывает вне навмеша), к
		// человеку — ближе (подойти и послушать)
		const float arrive = a_command.value("arrive", 0.0f);
		const auto item = a_command.value("item", RE::FormID{ 0 });  // give: базовый предмет
		const int  count = a_command.value("count", 1);              // give: сколько
		SKSE::GetTaskInterface()->AddTask([id = std::move(id), action = std::move(action), target, file = std::move(file),
											  local, stay, lead, arrive, item, count] {
			const auto actor = voice::Gosling();
			if (!actor) {
				Report(id, "no_gosling");
				return;
			}
			const auto vm = RE::BSScript::Internal::VirtualMachine::GetSingleton();
			RE::BSTSmartPointer<RE::BSScript::IStackCallbackFunctor> callback;
			bool dispatched = false;
			const auto ref = target ? RE::TESForm::LookupByID<RE::TESObjectREFR>(target) :
			                 !file.empty()  ? RE::TESDataHandler::GetSingleton()->LookupForm<RE::TESObjectREFR>(local, file) :
			                                  nullptr;
			if ((action == "go_to" || action == "map" || action == "take") && !ref) {
				Report(id, "no_target");
				return;
			}
			if (action == "go_to") {
				logs::info("Приказ {}: {} к {:08X}, {} м от Гослинга, потом {}", id, lead ? "вести" : "идти", target,
					actor->GetPosition().GetDistance(ref->GetPosition()) / 70.0f, stay ? "ждать там" : "вернуться");
				const auto args = RE::MakeFunctionArguments(static_cast<RE::Actor*>(actor), static_cast<RE::TESObjectREFR*>(ref),
					bool(stay), bool(lead), std::string(id), float(arrive * 70.0f));
				dispatched = vm && vm->DispatchStaticCall("GoslingOrders", "GoTo", args, callback);
			} else if (action == "map") {
				const auto args = RE::MakeFunctionArguments(static_cast<RE::TESObjectREFR*>(ref), std::string(id));
				dispatched = vm && vm->DispatchStaticCall("GoslingOrders", "MarkOnMap", args, callback);
			} else if (action == "take") {
				logs::info("Приказ {}: взять «{}» ({:08X})", id, net::ToUtf8(ref->GetDisplayFullName()), ref->GetFormID());
				if (const auto base = ref->GetBaseObject(); base && base->Is(RE::FormType::Container)) {
					takes[id] = ref->GetFormID();
				}
				const auto args = RE::MakeFunctionArguments(static_cast<RE::Actor*>(actor), static_cast<RE::TESObjectREFR*>(ref),
					std::string(id));
				dispatched = vm && vm->DispatchStaticCall("GoslingOrders", "Take", args, callback);
			} else if (action == "give") {
				const auto form = RE::TESForm::LookupByID(item);
				if (!form) {
					Report(id, "no_target");
					return;
				}
				events::ExpectHandover();  // что сейчас придёт от него игроку — «отдал сам», а не «Владос забрал»
				const auto args = RE::MakeFunctionArguments(static_cast<RE::Actor*>(actor), static_cast<RE::TESForm*>(form),
					std::int32_t(count), std::string(id));
				dispatched = vm && vm->DispatchStaticCall("GoslingOrders", "Give", args, callback);
			} else {
				const auto args = RE::MakeFunctionArguments(static_cast<RE::Actor*>(actor), std::string(action), std::string(id));
				dispatched = vm && vm->DispatchStaticCall("GoslingOrders", "Order", args, callback);
			}
			if (!dispatched) {
				Report(id, "papyrus");
			}
		});
	}

	void StopTravel()
	{
		const auto vm = RE::BSScript::Internal::VirtualMachine::GetSingleton();
		RE::BSTSmartPointer<RE::BSScript::IStackCallbackFunctor> callback;
		const auto args = RE::MakeFunctionArguments();
		if (!vm || !vm->DispatchStaticCall("GoslingOrders", "StopTravel", args, callback)) {
			logs::warn("Поход: остановить при загрузке не вышло (скрипт не вызван)");
		}
	}
}
