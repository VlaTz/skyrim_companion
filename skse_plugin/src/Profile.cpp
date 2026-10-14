#include "Profile.h"

#include "Net.h"

namespace profile
{
	namespace
	{
		constexpr std::size_t kTopSkills = 5;

		constexpr std::array<std::pair<RE::ActorValue, const char*>, 18> kSkills{ {
			{ RE::ActorValue::kOneHanded, "одноручное оружие" },
			{ RE::ActorValue::kTwoHanded, "двуручное оружие" },
			{ RE::ActorValue::kArchery, "стрельба" },
			{ RE::ActorValue::kBlock, "блок" },
			{ RE::ActorValue::kSmithing, "кузнечное дело" },
			{ RE::ActorValue::kHeavyArmor, "тяжёлая броня" },
			{ RE::ActorValue::kLightArmor, "лёгкая броня" },
			{ RE::ActorValue::kPickpocket, "карманные кражи" },
			{ RE::ActorValue::kLockpicking, "взлом" },
			{ RE::ActorValue::kSneak, "скрытность" },
			{ RE::ActorValue::kAlchemy, "алхимия" },
			{ RE::ActorValue::kSpeech, "красноречие" },
			{ RE::ActorValue::kAlteration, "изменение" },
			{ RE::ActorValue::kConjuration, "колдовство" },
			{ RE::ActorValue::kDestruction, "разрушение" },
			{ RE::ActorValue::kIllusion, "иллюзия" },
			{ RE::ActorValue::kRestoration, "восстановление" },
			{ RE::ActorValue::kEnchanting, "зачарование" },
		} };

		// Крупные квесты: главный, гильдии, даэдрические, война, DLC (поручения и мелочь — нет).
		bool IsMajor(RE::QUEST_DATA::Type a_type)
		{
			using T = RE::QUEST_DATA::Type;
			switch (a_type) {
			case T::kMainQuest:
			case T::kMagesGuild:
			case T::kThievesGuild:
			case T::kDarkBrotherhood:
			case T::kCompanionsQuest:
			case T::kDaedric:
			case T::kCivilWar:
			case T::kDLC01_Vampire:
			case T::kDLC02_Dragonborn:
				return true;
			default:
				return false;
			}
		}

		json FormRef(const RE::TESForm* a_form)
		{
			const auto file = a_form->GetFile(0);
			return { { "file", file ? std::string(file->GetFilename()) : std::string{} },
				{ "form", a_form->GetLocalFormID() } };
		}
	}

	json Collect(RE::PlayerCharacter* a_player)
	{
		json       out;
		const auto base = a_player->GetActorBase();
		if (const auto race = a_player->GetRace()) {
			out["race"] = net::ToUtf8(race->GetName());
		}
		out["female"] = base && base->IsFemale();
		out["level"] = a_player->GetLevel();

		std::vector<std::pair<float, const char*>> skills;
		for (const auto& [av, name] : kSkills) {
			skills.emplace_back(a_player->AsActorValueOwner()->GetBaseActorValue(av), name);
		}
		std::ranges::sort(skills, std::greater{}, &std::pair<float, const char*>::first);
		json top = json::array();
		for (std::size_t i = 0; i < (std::min)(kTopSkills, skills.size()); ++i) {
			top.push_back({ { "name", skills[i].second }, { "value", static_cast<int>(skills[i].first) } });
		}
		out["skills"] = std::move(top);

		// Фракции: запись NPC игрока + изменения в игре (ExtraFactionChanges) — только чтение полей.
		// Ранг < 0 — не состоит (вышел, выгнали).
		json factions = json::array();
		a_player->VisitFactions([&](RE::TESFaction* a_faction, std::int8_t a_rank) {
			if (a_faction && a_rank >= 0) {
				auto ref = FormRef(a_faction);
				ref["rank"] = a_rank;
				factions.push_back(std::move(ref));
			}
			return false;
		});
		out["factions"] = std::move(factions);

		// Награды за голову: crimeGoldMap игрока (поле; смещение для 1.7.x CommonLib учитывает сам).
		json crime = json::array();
		for (const auto& entry : a_player->GetCrimeValue().crimeGoldMap) {
			const auto gold = entry.second.violentCur + entry.second.nonViolentCur;
			if (entry.first && gold >= 1.0f) {
				auto ref = FormRef(entry.first);
				ref["gold"] = static_cast<int>(gold);
				crime.push_back(std::move(ref));
			}
		}
		out["crime"] = std::move(crime);

		json done = json::array();
		if (const auto handler = RE::TESDataHandler::GetSingleton()) {
			for (const auto quest : handler->GetFormArray<RE::TESQuest>()) {
				if (quest && quest->IsCompleted() && IsMajor(quest->GetType())) {
					if (auto name = net::ToUtf8(quest->GetName()); !name.empty()) {
						done.push_back({ { "name", std::move(name) }, { "type", static_cast<int>(quest->GetType()) } });
					}
				}
			}
		}
		out["quests_done"] = std::move(done);
		return out;
	}
}
