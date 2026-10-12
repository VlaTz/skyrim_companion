#include "Player.h"

#include "Net.h"

namespace player
{
	namespace
	{
		constexpr std::size_t kTopPerCategory = 6;  // самые ценные предметы каждой категории
		constexpr std::size_t kMaxEffects = 12;

		const char* Category(RE::TESBoundObject* a_object)
		{
			switch (a_object->GetFormType()) {
			case RE::FormType::Weapon:
				return "оружие";
			case RE::FormType::Armor:
				return "броня и одежда";
			case RE::FormType::Ammo:
				return "стрелы";
			case RE::FormType::AlchemyItem:
				{
					const auto potion = a_object->As<RE::AlchemyItem>();
					return potion->IsFood() ? "еда" : potion->IsPoison() ? "яды" : "зелья";
				}
			case RE::FormType::Ingredient:
				return "ингредиенты";
			case RE::FormType::Book:
				return "книги";
			case RE::FormType::KeyMaster:
				return "ключи";
			case RE::FormType::SoulGem:
				return "камни душ";
			case RE::FormType::Scroll:
				return "свитки";
			default:
				return "разное";
			}
		}

		std::int32_t BaseValue(RE::TESBoundObject* a_object)
		{
			// Базовая цена из записи (без зачарований): точную считает функция игры по адресу — не зовём.
			const auto value = a_object->As<RE::TESValueForm>();
			return value ? value->value : 0;
		}

		struct Item
		{
			std::string  name;
			std::int32_t count;
			std::int32_t value;
		};
	}

	json Collect(RE::PlayerCharacter* a_player)
	{
		json out;
		std::map<std::string, std::vector<Item>> categories;
		std::map<std::string, std::int32_t>      totals;
		json                                     worn = json::array();

		// a_noInit = true: читаем уже готовый инвентарь, без инициализации (она — функция игры по адресу).
		const auto inventory = a_player->GetInventory([](RE::TESBoundObject&) { return true; }, true);
		for (const auto& [object, data] : inventory) {
			const auto& [count, entry] = data;
			if (!object || count <= 0 || object->IsGold()) {
				continue;
			}
			auto name = net::ToUtf8(object->GetName());
			if (name.empty()) {
				continue;
			}
			if (entry && entry->IsWorn()) {
				worn.push_back(name);
			}
			const std::string category = Category(object);
			totals[category] += count;
			categories[category].push_back({ std::move(name), count, BaseValue(object) });
		}

		json bag = json::object();
		for (auto& [category, items] : categories) {
			std::ranges::sort(items, std::greater{}, [](const Item& a_item) { return a_item.value; });
			json top = json::array();
			for (const auto& item : items | std::views::take(kTopPerCategory)) {
				top.push_back({ { "name", item.name }, { "count", item.count }, { "value", item.value } });
			}
			bag[category] = { { "total", totals[category] }, { "kinds", items.size() }, { "top", std::move(top) } };
		}

		json effects = json::array();
		if (const auto target = a_player->AsMagicTarget()) {
			if (const auto list = target->GetActiveEffectList()) {
				std::set<std::string> seen;
				for (const auto effect : *list) {
					if (!effect || effect->flags.any(RE::ActiveEffect::Flag::kInactive, RE::ActiveEffect::Flag::kDispelled)) {
						continue;
					}
					const auto base = effect->GetBaseObject();
					if (!base || base->data.flags.any(RE::EffectSetting::EffectSettingData::Flag::kHideInUI)) {
						continue;
					}
					auto name = net::ToUtf8(base->GetFullName());
					if (!name.empty() && seen.insert(name).second && effects.size() < kMaxEffects) {
						effects.push_back({ { "name", std::move(name) }, { "harmful", base->IsDetrimental() } });
					}
				}
			}
		}

		const auto av = a_player->AsActorValueOwner();
		out["gold"] = a_player->GetGoldAmount(true);
		out["level"] = a_player->GetLevel();
		out["health"] = { av->GetActorValue(RE::ActorValue::kHealth), a_player->GetActorValueMax(RE::ActorValue::kHealth) };
		out["magicka"] = { av->GetActorValue(RE::ActorValue::kMagicka), a_player->GetActorValueMax(RE::ActorValue::kMagicka) };
		out["stamina"] = { av->GetActorValue(RE::ActorValue::kStamina), a_player->GetActorValueMax(RE::ActorValue::kStamina) };
		out["worn"] = std::move(worn);
		out["bag"] = std::move(bag);
		out["effects"] = std::move(effects);
		return out;
	}
}
