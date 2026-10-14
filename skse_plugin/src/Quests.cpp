#include "Quests.h"

#include <set>

#include "Net.h"

namespace quests
{
	namespace
	{
		struct Entry
		{
			std::vector<std::string> objectives;
			const RE::TESQuestStageItem* log = nullptr;  // последняя (по этапу) запись журнала
		};

		std::uint16_t StageOf(const RE::TESQuestStageItem* a_item)
		{
			return a_item->owningStage ? a_item->owningStage->data.index : 0;
		}

		std::string FileName(const RE::TESFile* a_file)
		{
			return a_file ? std::string(a_file->GetFilename()) : std::string{};
		}

		// Имена тех, кто подставляется в текст целей и журнала: <Alias=Target> -> «Арнгейр».
		json Aliases(RE::TESQuest* a_quest)
		{
			json out = json::object();
			for (const auto alias : a_quest->aliases) {
				if (!alias || alias->GetVMTypeID() != RE::BGSRefAlias::VMTYPEID) {
					continue;
				}
				const auto ref = static_cast<RE::BGSRefAlias*>(alias)->GetReference();
				if (ref) {
					if (auto name = net::ToUtf8(ref->GetDisplayFullName()); !name.empty()) {
						out[std::string(alias->aliasName.c_str())] = std::move(name);
					}
				}
			}
			return out;
		}
	}

	json Collect()
	{
		const auto player = RE::PlayerCharacter::GetSingleton();
		if (!player) {
			return json::array();
		}
		auto& runtime = player->GetPlayerRuntimeData();

		// Активный квест в журнале = у него есть показанная и ещё не выполненная цель.
		std::map<RE::TESQuest*, Entry> active;
		for (const auto& instanced : runtime.objectives) {
			const auto objective = instanced.Objective;
			if (!objective || !objective->ownerQuest ||
				instanced.InstanceState != RE::QUEST_OBJECTIVE_STATE::kDisplayed) {
				continue;
			}
			auto& entry = active[objective->ownerQuest];
			auto  text = net::ToUtf8(objective->displayText.c_str());
			if (!text.empty() && std::ranges::find(entry.objectives, text) == entry.objectives.end()) {
				entry.objectives.push_back(std::move(text));
			}
		}

		for (const auto item : runtime.questLog) {
			if (item && item->hasLogEntry && item->owner) {
				if (const auto it = active.find(item->owner); it != active.end()) {
					if (!it->second.log || StageOf(item) >= StageOf(it->second.log)) {
						it->second.log = item;
					}
				}
			}
		}

		json out = json::array();
		for (const auto& [quest, entry] : active) {
			json q = {
				{ "id", std::format("{:08X}", quest->GetFormID()) },
				{ "name", net::ToUtf8(quest->GetName()) },
				{ "type", static_cast<int>(quest->GetType()) },
				{ "stage", quest->GetCurrentStageID() },
				{ "objectives", entry.objectives },
				{ "aliases", Aliases(quest) },
				{ "tracked", quest->IsActive() },  // отмечен активным в журнале (флаг квеста, только чтение)
			};
			if (entry.log) {
				// Строку журнала ищем в таблице файла, где квест определён последним, затем — первым.
				q["log"] = { { "id", entry.log->logEntry.id },
					{ "files", { FileName(quest->GetFile(-1)), FileName(quest->GetFile(0)) } } };
			}
			out.push_back(std::move(q));
		}
		return out;
	}

	json Completed()
	{
		const auto player = RE::PlayerCharacter::GetSingleton();
		std::set<RE::FormID> done;
		if (player) {
			for (const auto item : player->GetPlayerRuntimeData().questLog) {
				if (item && item->owner && item->owner->IsCompleted()) {
					done.insert(item->owner->GetFormID());
				}
			}
		}
		json out = json::array();
		for (const auto id : done) {
			out.push_back(std::format("{:08X}", id));
		}
		return out;
	}
}
