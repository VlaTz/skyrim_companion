#include "Events.h"

#include "Net.h"
#include "Orders.h"
#include "Player.h"
#include "Profile.h"
#include "Quests.h"
#include "Voice.h"

namespace events
{
	namespace
	{
		using namespace std::chrono_literals;
		using Clock = std::chrono::steady_clock;

		constexpr auto        kTick = 1s;                // как часто проверяем состояние
		constexpr auto        kStatusEvery = 2s;         // снимок status — не реже (здоровье, кто рядом, собеседник)
		constexpr auto        kPlayerMinGap = 3s;        // подробности об игроке — не чаще (разграбление сундука)
		constexpr auto        kPlayerEvery = 30s;        // и не реже (эффекты меняются без событий)
		constexpr auto        kProfileEvery = 60s;       // профиль Владоса (фракции, розыск, квесты) — не реже
		constexpr float       kNearbyRadius = 2500.0f;   // ~35 м
		constexpr std::size_t kNearbyMax = 10;
		constexpr float       kThreatRadius = 10000.0f;  // ~140 м: враги дальше «рядом» — дракон в небе, великан в поле
		constexpr std::size_t kThreatMax = 3;
		constexpr float       kGoslingApart = 2100.0f;   // ~30 м: дальше Гослинг не слышит игрока — шлём, кто вокруг него
		constexpr float       kLootRadius = 700.0f;      // ~10 м: вещи и сундуки рядом с Гослингом — что он может взять
		constexpr std::size_t kLootMax = 12;
		constexpr float       kDoorRadius = 10000.0f;    // ~140 м: двери в здания вокруг Гослинга — поселение целиком
		constexpr std::size_t kDoorMax = 100;  // все здания поселения: видеть надо всё
		constexpr float       kHearRadius = 1750.0f;     // ~25 м: реплики NPC дальше Гослинг не слышит
		constexpr float       kTouchRadius = 300.0f;     // ~4 м: замок открылся рядом с игроком — это он открыл
		constexpr float       kUnitsPerMeter = 70.0f;
		constexpr float       kFleeSpeed = 245.0f;       // ед./с (~3,5 м/с): собеседник бежит, а не идёт
		constexpr int         kFleeTicks = 2;            // столько проверок подряд — убегает
		constexpr auto        kFleeRepeat = 20s;         // «убегает» про одного и того же — не чаще
		constexpr auto        kPartnerFor = 300s;        // за тем, с кем говорил игрок, следим 5 минут
		constexpr auto        kActivateRepeat = 3s;      // повторная активация того же предмета — не событие
		constexpr auto        kLockpickAfter = 2s;       // замок открылся так скоро после меню взлома — взломан
		constexpr RE::FormID  kPlayerRef = 0x14;

		// Всё ниже трогается только из игрового потока (обработчики событий и задачи AddTask).
		struct Last
		{
			bool        valid = false;
			bool        inCombat = false;
			bool        paused = false;  // меню, пауза, загрузка — мира на экране не видно
			std::string location;
			std::string cell;
		} last;

		Clock::time_point lastStatus{};
		bool              forceStatus = false;
		bool              inGame = false;  // загружена игра, а не главное меню
		bool              inDialogue = false;  // игрок в меню разговора с NPC

		std::string saveCharacter;  // ID персонажа из имени загружаемого сейва («Autosave1_02CCEDA2_…»)

		const RE::TESWorldSpace* markersWorld = nullptr;  // для какого мира последний раз слали маркеры карты
		bool                     markersDirty = true;     // открыто новое место — разослать заново
		bool                     questsDirty = true;      // сменился этап или цель квеста — разослать журнал
		bool                     playerDirty = true;      // сменились инвентарь или экипировка
		Clock::time_point        lastPlayer{};
		bool                     profileDirty = true;     // загрузка, сменился этап квеста — разослать профиль
		Clock::time_point        lastProfile{};

		// Собеседник: с кем игрок недавно заговорил (активировал живого NPC). Следим, жив ли, враждебен ли,
		// не убегает ли (Арвел вырвался из паутины и побежал). По номеру формы — без функций игры по адресу.
		struct Partner
		{
			RE::FormID        id = 0;
			std::string       name;
			Clock::time_point since{};
			RE::NiPoint3      lastPos{};
			Clock::time_point lastAt{};
			int               runTicks = 0;
			Clock::time_point fledAt{};
		} partner;

		RE::FormID        lastActivated = 0;
		Clock::time_point lastActivatedAt{};
		RE::FormID        pendingLock = 0;  // замок открылся, а активации ещё не было (порядок событий не гарантирован)
		Clock::time_point pendingLockAt{};
		std::string       lastShout;  // крик и когда — один крик игра может прислать несколькими заклинаниями
		Clock::time_point lastShoutAt{};
		bool              lockpicking = false;  // открыто меню взлома замка
		Clock::time_point lockpickClosed{};

		// Обмен вещами с Гослингом: игрок перекладывает пачкой — копим и шлём одним событием через kExchangeQuiet.
		struct Exchange
		{
			std::map<RE::FormID, int> given;  // базовый предмет -> сколько игрок отдал Гослингу
			std::map<RE::FormID, int> taken;  // и сколько забрал у него
			std::map<RE::FormID, int> handed;  // сколько Гослинг отдал сам — находки вылазки (приказ give)
			Clock::time_point         lastAt{};
		} exchange;
		constexpr auto kExchangeQuiet = 2s;
		// Гослинг отдаёт находки (ExpectHandover): что придёт от него игроку до этого момента — «отдал сам», а не
		// «Владос забрал» (то огорчает). Оба — в игровом потоке.
		Clock::time_point handoverUntil{};
		constexpr auto    kHandoverFor = 10s;

		void Post(std::string_view a_type, json a_data = json::object())
		{
			net::Post({ { "type", a_type }, { "data", std::move(a_data) } });
		}

		std::string Name(const RE::TESForm* a_form)
		{
			return a_form ? net::ToUtf8(a_form->GetName()) : std::string{};
		}

		std::string Name(RE::TESObjectREFR* a_ref)
		{
			return a_ref ? net::ToUtf8(a_ref->GetDisplayFullName()) : std::string{};
		}

		const RE::BGSLocation* FindHold(const RE::BGSLocation* a_loc)
		{
			for (; a_loc; a_loc = a_loc->parentLoc) {
				if (a_loc->HasKeywordString("LocTypeHold")) {
					return a_loc;
				}
			}
			return nullptr;
		}

		// Чья это игра — память Гослинга своя у каждого персонажа. Надёжнее всего ID из имени сейва; поле
		// BGSSaveLoadManager::currentCharacterID — запасной путь (на 1.7.x смещение не проверено, сверяем в логе).
		std::string CharacterId()
		{
			if (!saveCharacter.empty()) {
				return saveCharacter;
			}
			const auto manager = RE::BGSSaveLoadManager::GetSingleton();
			return manager && manager->currentCharacterID ? std::format("{:08X}", manager->currentCharacterID) : std::string{};
		}

		float DaysPassed()
		{
			const auto calendar = RE::Calendar::GetSingleton();
			return calendar ? calendar->GetDaysPassed() : 0.0f;
		}

		std::string Weather()
		{
			const auto sky = RE::Sky::GetSingleton();
			const auto weather = sky ? sky->currentWeather : nullptr;
			if (!weather) {
				return {};
			}
			using Flag = RE::TESWeather::WeatherDataFlag;
			const auto& flags = weather->data.flags;
			if (flags.any(Flag::kSnow)) {
				return "снег";
			}
			if (flags.any(Flag::kRainy)) {
				return "дождь";
			}
			if (flags.any(Flag::kCloudy)) {
				return "облачно";
			}
			if (flags.any(Flag::kPleasant)) {
				return "ясно";
			}
			return {};
		}

		// Мир, чья карта используется: у городов (Вайтран и др.) свой мир, но карта и координаты — Скайрима.
		RE::TESWorldSpace* MapWorld(RE::TESWorldSpace* a_world)
		{
			while (a_world && a_world->parentWorld &&
				   a_world->parentUseFlags.any(RE::TESWorldSpace::ParentUseFlag::kUseMapData)) {
				a_world = a_world->parentWorld;
			}
			return a_world;
		}

		// Все маркеры карты мира: что из них можно показывать Гослингу, решает сервис.
		// Выключенные пропускаем: их места пока нет в мире (участок HearthFire до покупки земли, место квеста до его
		// начала) — пути туда игра не строит. Включит игра — придут со следующей рассылкой.
		void SendMarkers(RE::TESWorldSpace* a_world)
		{
			json markers = json::array();
			if (const auto cell = a_world->persistentCell) {
				cell->ForEachReference([&](RE::TESObjectREFR* a_ref) {
					const auto extra = a_ref && !a_ref->IsDisabled() ? a_ref->extraList.GetByType<RE::ExtraMapMarker>() : nullptr;
					const auto data = extra ? extra->mapData : nullptr;
					if (data && data->type.get() < RE::MARKER_TYPE::kTotalLocationTypes) {
						if (auto name = net::ToUtf8(data->locationName.GetFullName()); !name.empty()) {
							const auto pos = a_ref->GetPosition();
							// form — сам маркер (постоянная ссылка): к нему можно послать Гослинга (приказ go_to).
							markers.push_back({ { "name", std::move(name) },
								{ "form", a_ref->GetFormID() },
								{ "type", static_cast<int>(data->type.get()) },
								{ "x", pos.x }, { "y", pos.y },
								{ "visible", data->flags.any(RE::MapMarkerData::Flag::kVisible) },
								{ "travel", data->flags.any(RE::MapMarkerData::Flag::kCanTravelTo) } });
						}
					}
					return RE::BSContainer::ForEachResult::kContinue;
				});
			}
			logs::info("Маркеры карты: {} шт. ({})", markers.size(), Name(a_world));
			Post("map_markers", { { "worldspace", Name(a_world) }, { "markers", std::move(markers) } });
		}

		struct Near
		{
			float       distance;
			RE::Actor*  actor;
		};

		// Единственный в игре (Арвел, Белетор), а не «Драугр» из шаблона: флаг записи NPC, только чтение поля.
		bool IsUnique(RE::Actor* a_actor)
		{
			const auto base = a_actor ? a_actor->GetActorBase() : nullptr;
			return base && base->IsUnique();
		}

		// Враг: враждебен игроку. Существо (не людская раса) — ещё и агрессивно: олени, кролики, лисы игра тоже
		// считает «враждебными», хотя они убегают («начался бой: Олень, Олень, Мирмулнир»). Агрессия — значение
		// актёра (как здоровье игрока), раса — поле записи.
		bool Hostile(RE::Actor* a_actor, RE::PlayerCharacter* a_player)
		{
			if (!a_actor->IsHostileToActor(a_player)) {
				return false;
			}
			const auto race = a_actor->GetActorRuntimeData().race;
			const bool creature = race && !race->data.flags.any(RE::RACE_DATA::Flag::kPlayable);
			return !creature || a_actor->AsActorValueOwner()->GetActorValue(RE::ActorValue::kAggression) > 0.0f;
		}

		// Дерётся с нами: в бою, и цель — игрок или его спутник. Враждебный великан в стороне — не участник боя.
		bool FightsUs(RE::Actor* a_actor, RE::PlayerCharacter* a_player)
		{
			if (!a_actor->IsInCombat()) {
				return false;
			}
			const auto target = a_actor->GetActorRuntimeData().currentCombatTarget.get();
			return target && (target.get() == a_player || target->IsPlayerTeammate());
		}

		struct Around
		{
			std::vector<Near> nearby;   // ближе kNearbyRadius, kNearbyMax ближайших
			std::vector<Near> threats;  // живые враги дальше — до kThreatRadius, kThreatMax ближайших
		};

		Around ActorsAround(RE::PlayerCharacter* a_player)
		{
			Around around;
			RE::ProcessLists::GetSingleton()->ForEachHighActor([&](RE::Actor* a_actor) {
				if (a_actor && a_actor != a_player) {
					const float distance = a_player->GetDistance(a_actor);
					if (distance < kNearbyRadius) {
						around.nearby.push_back({ distance, a_actor });
					} else if (distance < kThreatRadius && !a_actor->IsDead() && Hostile(a_actor, a_player)) {
						around.threats.push_back({ distance, a_actor });
					}
				}
				return RE::BSContainer::ForEachResult::kContinue;
			});
			for (auto* list : { &around.nearby, &around.threats }) {
				std::ranges::sort(*list, {}, &Near::distance);
			}
			if (around.nearby.size() > kNearbyMax) {
				around.nearby.resize(kNearbyMax);
			}
			if (around.threats.size() > kThreatMax) {
				around.threats.resize(kThreatMax);
			}
			return around;
		}

		// Раса, если она не людская («Дракон», «Драугр», «Великан»): по ней сервис помнит, кого вы уже встречали.
		// У людей и эльфов (играбельные расы) — пусто: «впервые видишь норда» не бывает. Только чтение полей.
		std::string RaceOf(RE::Actor* a_actor)
		{
			const auto race = a_actor ? a_actor->GetActorRuntimeData().race : nullptr;
			if (!race || race->data.flags.any(RE::RACE_DATA::Flag::kPlayable)) {
				return {};
			}
			return Name(race);
		}

		// Тип голоса NPC (номер формы): разговор с Гослингом — реплика NPC его голосом (папка звуков — по типу голоса).
		// Поле voiceType записи NPC, а у кого его нет — у шаблона (Use Traits). GetObjectVoiceType() отдаёт не тип голоса
		// (у Гослинга — E000007F вместо 00013AD1, лог 21.10): по нему голос NPC не находился (24.10).
		RE::FormID VoiceOf(RE::Actor* a_actor)
		{
			int depth = 0;
			for (auto npc = a_actor ? a_actor->GetActorBase() : nullptr; npc && depth < 8; ++depth,
					  npc = npc->baseTemplateForm ? npc->baseTemplateForm->As<RE::TESNPC>() : nullptr) {
				if (npc->voiceType) {
					return npc->voiceType->GetFormID();
				}
			}
			return 0;
		}

		// По позициям, в одном мире или одной ячейке (24.10: Гослинг в 64 м, а в списке — «~3 м от тебя»: расстояния не
		// было, и бралось расстояние от игрока).
		json GoslingDistance(RE::Actor* a_actor)
		{
			const auto gosling = voice::Gosling();
			if (!gosling || gosling->IsDisabled()) {
				return nullptr;
			}
			const auto world = gosling->GetWorldspace();
			const bool together = world ? world == a_actor->GetWorldspace() :
			                              gosling->GetParentCell() && gosling->GetParentCell() == a_actor->GetParentCell();
			if (!together) {
				return nullptr;
			}
			return std::round(gosling->GetPosition().GetDistance(a_actor->GetPosition()) / kUnitsPerMeter);
		}

		json ActorJson(RE::PlayerCharacter* a_player, const Near& a_near)
		{
			return { { "name", Name(a_near.actor) },
				{ "hostile", Hostile(a_near.actor, a_player) || FightsUs(a_near.actor, a_player) },
				{ "dead", a_near.actor->IsDead() },
				{ "follower", a_near.actor->IsPlayerTeammate() },
				{ "unique", IsUnique(a_near.actor) },
				{ "race", RaceOf(a_near.actor) },
				{ "level", a_near.actor->GetLevel() },  // насколько опасен — сравнивают с уровнем игрока
				{ "distance", std::round(a_near.distance / kUnitsPerMeter) },
				{ "form", a_near.actor->GetFormID() },  // к нему можно подойти (исследователь: «послушать»)
				{ "voice", VoiceOf(a_near.actor) },
				// от Гослинга (м): заговорить можно, когда ОН рядом с человеком, а игрок может стоять поодаль (24.10)
				{ "gosling_distance", GoslingDistance(a_near.actor) } };
		}

		// Чужое: у ссылки или у ячейки есть владелец (не игрок) — взять значит украсть. Только чтение данных.
		bool Owned(RE::TESObjectREFR* a_ref, RE::TESObjectCELL* a_cell)
		{
			for (const auto* list : { &a_ref->extraList, a_cell ? &a_cell->extraList : nullptr }) {
				const auto own = list ? list->GetByType<RE::ExtraOwnership>() : nullptr;
				if (own && own->owner && own->owner->GetFormID() != 0x7) {
					return true;
				}
			}
			return false;
		}

		// Квестовый предмет: его держит алиас задания с флагом «квестовый предмет» — не трогаем, Владос возьмёт сам.
		bool QuestObject(RE::TESObjectREFR* a_ref)
		{
			const auto aliases = a_ref->extraList.GetByType<RE::ExtraAliasInstanceArray>();
			if (!aliases) {
				return false;
			}
			for (const auto* data : aliases->aliases) {
				if (data && data->alias && data->alias->flags.any(RE::BGSBaseAlias::FLAGS::kQuestObject)) {
					return true;
				}
			}
			return false;
		}

		bool Locked(RE::TESObjectREFR* a_ref)
		{
			const auto lock = a_ref->extraList.GetByType<RE::ExtraLock>();
			return lock && lock->lock && lock->lock->IsLocked();
		}

		// Что можно взять рядом с Гослингом (исследователь решает, companion/explorer.py): предметы и контейнеры в его
		// ячейке ближе kLootRadius — с ценой, «чужое», «по заданию», «заперто». Сундук — сколько в нём вещей.
		json ItemsAround(RE::Actor* a_actor)
		{
			json items = json::array();
			const auto cell = a_actor->GetParentCell();
			if (!cell) {
				return items;
			}
			std::vector<RE::TESObjectREFR*> refs;
			cell->ForEachReferenceInRange(a_actor->GetPosition(), kLootRadius, [&](RE::TESObjectREFR* a_ref) {
				const auto base = a_ref ? a_ref->GetBaseObject() : nullptr;
				if (base && a_ref != a_actor && !a_ref->IsDisabled() && !a_ref->IsDeleted() &&
					base->Is(RE::FormType::Weapon, RE::FormType::Armor, RE::FormType::Book, RE::FormType::Misc,
						RE::FormType::AlchemyItem, RE::FormType::Ingredient, RE::FormType::SoulGem, RE::FormType::Scroll,
						RE::FormType::KeyMaster, RE::FormType::Container)) {
					refs.push_back(a_ref);
				}
				return RE::BSContainer::ForEachResult::kContinue;
			});
			std::ranges::sort(refs, {}, [&](RE::TESObjectREFR* a_ref) { return a_actor->GetDistance(a_ref); });
			for (const auto ref : refs) {
				if (items.size() >= kLootMax || Name(ref).empty()) {
					continue;
				}
				const auto base = ref->GetBaseObject();
				const auto value = base->As<RE::TESValueForm>();
				json item = { { "name", Name(ref) }, { "form", ref->GetFormID() },
					{ "kind", base->Is(RE::FormType::Container) ? "container" : "item" },
					{ "value", value ? value->value : 0 }, { "owned", Owned(ref, cell) }, { "quest", QuestObject(ref) },
					{ "distance", std::round(a_actor->GetDistance(ref) / kUnitsPerMeter) } };
				if (base->Is(RE::FormType::Container)) {
					std::int32_t count = 0;
					for (const auto& [object, data] : ref->GetInventory([](RE::TESBoundObject&) { return true; }, true)) {
						count += data.first > 0 ? data.first : 0;
					}
					item["locked"] = Locked(ref);
					item["count"] = count;  // пустой сундук — незачем
				}
				items.push_back(std::move(item));
			}
			return items;
		}

		// Двери вокруг Гослинга, ведущие в другие локации (здания поселения): куда (локация по ту сторону), заперта ли и
		// ближайшая дверь (form — к ней можно послать: «у входа», снаружи, где Владос рядом и мир живой). Исследователь
		// (companion/explorer.py) по ним не ведёт в запертое: пакет в запертую дверь не пройдёт, а взламывать Гослинг не
		// умеет. Одна локация — несколько дверей: открыта хоть одна — открыто. inside — та же дверь изнутри (куда ведёт):
		// «зайти внутрь» — к ней, встать сразу за дверью и ждать Владоса (середина здания — где попало внутри, 24.10).
		struct Door
		{
			bool         locked;
			float        distance;
			RE::FormID   form;
			RE::FormID   inside;
		};

		json DoorsAround(RE::Actor* a_actor)
		{
			std::map<std::string, Door> doors;  // локация -> её дверь (ближайшая)
			RE::TES::GetSingleton()->ForEachReferenceInRange(a_actor, kDoorRadius, [&](RE::TESObjectREFR* a_ref) {
				const auto base = a_ref ? a_ref->GetBaseObject() : nullptr;
				const auto teleport = base && base->Is(RE::FormType::Door) && !a_ref->IsDisabled() ?
				                          a_ref->extraList.GetByType<RE::ExtraTeleport>() :
				                          nullptr;
				const auto other = teleport && teleport->teleportData ? teleport->teleportData->linkedDoor.get() : nullptr;
				const auto cell = other ? (other->GetParentCell() ? other->GetParentCell() : other->GetSaveParentCell()) : nullptr;
				const auto loc = cell ? cell->GetLocation() : nullptr;
				if (loc && loc != a_actor->GetCurrentLocation()) {
					const bool  locked = Locked(a_ref);
					const float distance = a_actor->GetDistance(a_ref) / kUnitsPerMeter;
					auto [it, added] = doors.try_emplace(Name(loc), Door{ locked, distance, a_ref->GetFormID(), other->GetFormID() });
					if (!added) {
						const bool closer = distance < it->second.distance;
						it->second = { it->second.locked && locked, closer ? distance : it->second.distance,
							closer ? a_ref->GetFormID() : it->second.form, closer ? other->GetFormID() : it->second.inside };
					}
				}
				return RE::BSContainer::ForEachResult::kContinue;
			});
			json out = json::array();
			for (const auto& [name, door] : doors) {
				if (!name.empty() && out.size() < kDoorMax) {
					out.push_back({ { "to", name }, { "locked", door.locked }, { "distance", std::round(door.distance) },
						{ "form", door.form }, { "inside", door.inside } });
				}
			}
			return out;
		}

		// «Глаза Гослинга» — к его статусу: где он (место, ячейка, позиция на карте) и, когда он загружен, но не рядом с
		// игроком (послали, ждёт, отстал), — кто вокруг него и дерётся ли он. Рядом с игроком его окружение — то же, что
		// у игрока (nearby). Вне загруженной области игра его только двигает: место и позиция есть, людей и боёв нет.
		void GoslingView(RE::PlayerCharacter* a_player, json& a_out)
		{
			const auto actor = voice::Gosling();
			if (!actor || actor->IsDisabled()) {
				return;
			}
			// Выгруженный (вдали от игрока) бывает без ячейки — тогда ячейка «из сохранения», как в GetWorldspace:
			// игра двигает его и там. Без этого в переписке он не знал, где он (21.10).
			const auto cell = actor->GetParentCell() ? actor->GetParentCell() : actor->GetSaveParentCell();
			const auto loc = actor->GetCurrentLocation();
			if (loc || actor->Is3DLoaded()) {  // выгруженный без локации — неизвестно, а не «дикие земли»
				const auto hold = FindHold(loc);
				a_out["location"] = (!loc || loc == hold) ? std::string{} : Name(loc);
			}
			if (cell) {
				a_out["cell"] = Name(cell);
				a_out["interior"] = cell->IsInteriorCell();
			}
			if (actor->GetWorldspace() && actor->GetWorldspace() == a_player->GetWorldspace()) {
				const auto pos = actor->GetPosition();
				a_out["pos"] = { pos.x, pos.y };
			}
			if (!actor->Is3DLoaded()) {
				return;
			}
			a_out["items"] = ItemsAround(actor);  // и рядом с игроком: в «Веди» он берёт вещи, пока Владос рядом
			a_out["doors"] = DoorsAround(actor);
			if (a_player->GetDistance(actor) < kGoslingApart) {
				return;
			}
			std::vector<Near> others;
			RE::ProcessLists::GetSingleton()->ForEachHighActor([&](RE::Actor* a_actor) {
				if (a_actor && a_actor != actor && a_actor != a_player) {
					if (const float distance = actor->GetDistance(a_actor); distance < kNearbyRadius) {
						others.push_back({ distance, a_actor });
					}
				}
				return RE::BSContainer::ForEachResult::kContinue;
			});
			std::ranges::sort(others, {}, &Near::distance);
			json people = json::array();
			for (const auto& [distance, other] : others) {
				if (people.size() >= kNearbyMax || Name(other).empty()) {
					continue;
				}
				const auto target = other->GetActorRuntimeData().currentCombatTarget.get();
				people.push_back({ { "name", Name(other) },
					{ "hostile", Hostile(other, a_player) || (other->IsInCombat() && target && target.get() == actor) },
					{ "dead", other->IsDead() },
					{ "unique", IsUnique(other) },
					{ "race", RaceOf(other) },
					{ "level", other->GetLevel() },
					{ "distance", std::round(distance / kUnitsPerMeter) },
					{ "form", other->GetFormID() },
					{ "voice", VoiceOf(other) } });
			}
			a_out["in_combat"] = actor->IsInCombat();
			a_out["nearby"] = std::move(people);
		}

		// Собеседник для status: где он и что с ним; заодно — не убегает ли (событие npc_flees).
		json PartnerStatus(RE::PlayerCharacter* a_player, Clock::time_point a_now)
		{
			if (!partner.id || a_now - partner.since > kPartnerFor) {
				partner.id = 0;
				return nullptr;
			}
			const auto seconds = std::chrono::duration_cast<std::chrono::seconds>(a_now - partner.since).count();
			// Выгружен или в другой ячейке (ушёл за дверь, остался в другом помещении) — «не видно».
			const auto actor = RE::TESForm::LookupByID<RE::Actor>(partner.id);
			if (!actor || !actor->GetParentCell() || actor->GetWorldspace() != a_player->GetWorldspace() ||
				(actor->GetParentCell()->IsInteriorCell() && actor->GetParentCell() != a_player->GetParentCell())) {
				partner.lastAt = {};
				return { { "name", partner.name }, { "ago", seconds }, { "gone", true } };
			}
			const auto  pos = actor->GetPosition();
			const float dt = std::chrono::duration<float>(a_now - partner.lastAt).count();
			if (dt > 0.2f && partner.lastAt != Clock::time_point{}) {
				const auto step = pos - partner.lastPos;
				const bool away = step.Dot(pos - a_player->GetPosition()) > 0.0f;  // от игрока, а не на него
				partner.runTicks = (!actor->IsDead() && away && step.Length() / dt > kFleeSpeed) ? partner.runTicks + 1 : 0;
				if (partner.runTicks >= kFleeTicks && a_now - partner.fledAt > kFleeRepeat) {
					partner.fledAt = a_now;
					logs::info("Собеседник убегает: {}", partner.name);
					Post("npc_flees", { { "name", partner.name } });
				}
			}
			partner.lastPos = pos;
			partner.lastAt = a_now;
			return { { "name", partner.name }, { "ago", seconds },
				{ "distance", std::round(a_player->GetDistance(actor) / kUnitsPerMeter) },
				{ "hostile", Hostile(actor, a_player) }, { "dead", actor->IsDead() },
				{ "unique", IsUnique(actor) } };
		}

		// Ссылка с этим номером — NPC Гослинг.
		bool IsGoslingId(RE::FormID a_id)
		{
			return a_id && voice::IsGosling(RE::TESForm::LookupByID<RE::TESObjectREFR>(a_id));
		}

		// Предмет для сервиса: название, сколько, книга ли; файл и номер — по ним сервис найдёт текст книги.
		json ItemsJson(const std::map<RE::FormID, int>& a_items)
		{
			json out = json::array();
			for (const auto& [id, count] : a_items) {
				const auto form = RE::TESForm::LookupByID(id);
				const auto file = form ? form->GetFile(0) : nullptr;
				if (auto name = Name(form); count > 0 && !name.empty()) {
					out.push_back({ { "name", std::move(name) }, { "count", count },
						{ "book", form->GetFormType() == RE::FormType::Book },
						{ "file", file ? std::string(file->GetFilename()) : std::string{} },
						{ "form", form->GetLocalFormID() } });
				}
			}
			return out;
		}

		// Раз в kTick, в игровом потоке.
		void Tick()
		{
			const auto player = RE::PlayerCharacter::GetSingleton();
			const auto cell = player ? player->GetParentCell() : nullptr;
			if (!inGame || !cell) {
				return;
			}

			// Вне поселений локации нет (или это само владение) — шлём пустую: «дикие земли».
			const auto        loc = player->GetCurrentLocation();
			const auto        hold = FindHold(loc);
			const std::string location = (!loc || loc == hold) ? std::string{} : Name(loc);
			const std::string cellName = Name(cell);
			const bool        inCombat = player->IsInCombat();
			const auto        ui = RE::UI::GetSingleton();
			const bool        paused = ui && (ui->GameIsPaused() || ui->IsMenuOpen(RE::LoadingMenu::MENU_NAME));
			const auto        around = ActorsAround(player);
			const json        partnerNow = PartnerStatus(player, Clock::now());  // каждый тик — ловим бегство

			if (net::TakeReconnected()) {  // сервис перезапущен — он ничего не знает, шлём всё заново
				markersDirty = true;
				questsDirty = true;
				playerDirty = true;
				profileDirty = true;
				forceStatus = true;
			}

			if ((!exchange.given.empty() || !exchange.taken.empty() || !exchange.handed.empty()) &&
				Clock::now() - exchange.lastAt >= kExchangeQuiet) {
				auto given = ItemsJson(exchange.given);
				auto taken = ItemsJson(exchange.taken);
				auto handed = ItemsJson(exchange.handed);
				logs::info("Обмен с Гослингом: отдал {}, забрал {}, Гослинг отдал сам {}", given.size(), taken.size(),
					handed.size());
				exchange = {};
				Post("gosling_items", { { "given", std::move(given) }, { "taken", std::move(taken) },
										  { "handed", std::move(handed) } });
			}

			if (questsDirty) {
				questsDirty = false;
				auto active = quests::Collect();
				auto completed = quests::Completed();
				logs::info("Журнал: активных квестов {}, выполненных {}", active.size(), completed.size());
				Post("quests", { { "quests", std::move(active) }, { "completed", std::move(completed) } });
			}

			if (const auto now = Clock::now();
				(playerDirty && now - lastPlayer >= kPlayerMinGap) || now - lastPlayer >= kPlayerEvery) {
				playerDirty = false;
				lastPlayer = now;
				Post("player", player::Collect(player));
			}

			if (const auto now = Clock::now(); profileDirty || now - lastProfile >= kProfileEvery) {
				profileDirty = false;
				lastProfile = now;
				Post("profile", profile::Collect(player));
			}

			// В помещении мира нет — маркеры остаются от последней улицы.
			const auto world = MapWorld(player->GetWorldspace());
			if (world && (markersDirty || world != markersWorld)) {
				SendMarkers(world);
				markersWorld = world;
				markersDirty = false;
			}

			if (last.valid && inCombat != last.inCombat) {
				if (inCombat) {
					// С кем бой: кто дерётся с игроком или спутником — рядом и вдали (дракон кружит дальше 35 м).
					// Таких ещё нет (игрок напал первым) — враждебные рядом. Враждебные вдали, которые в бой не
					// вступили (великаны у лагеря), — не в счёт. races — кто они, если не люди; levels — уровень
					// (одноимённых — самый высокий), чтобы сравнить с игроком.
					json enemies = json::array();
					json races = json::object();
					json levels = json::object();
					bool anyFights = false;
					for (const auto* list : { &around.nearby, &around.threats }) {
						for (const auto& entry : *list) {
							anyFights = anyFights || (!entry.actor->IsDead() && FightsUs(entry.actor, player));
						}
					}
					for (const auto* list : { &around.nearby, &around.threats }) {
						for (const auto& [distance, actor] : *list) {
							const bool enemy = anyFights ? FightsUs(actor, player) :
							                               list == &around.nearby && Hostile(actor, player);
							if (enemy && !actor->IsDead()) {
								if (auto name = Name(actor); !name.empty()) {
									if (auto race = RaceOf(actor); !race.empty()) {
										races[name] = std::move(race);
									}
									const auto level = actor->GetLevel();
									if (!levels.contains(name) || levels[name].get<int>() < level) {
										levels[name] = level;
									}
									enemies.push_back(std::move(name));
								}
							}
						}
					}
					Post("combat_started", { { "enemies", std::move(enemies) }, { "races", std::move(races) },
											   { "levels", std::move(levels) } });
				} else {
					Post("combat_ended");
				}
			}

			const auto now = Clock::now();
			const bool changed = !last.valid || inCombat != last.inCombat || paused != last.paused ||
			                     location != last.location || cellName != last.cell;
			if (changed || forceStatus || now - lastStatus >= kStatusEvery) {
				// Строки шлём всегда: пустая значит «нет» (иначе сервис оставит старое значение).
				json       data;
				const bool interior = cell->IsInteriorCell();
				data["location"] = location;
				data["hold"] = Name(hold);
				data["cell"] = cellName;
				data["interior"] = interior;
				data["weather"] = interior ? std::string{} : Weather();
				if (world && !interior) {  // координаты помещений — свои, к карте не относятся
					const auto pos = player->GetPosition();
					data["pos"] = { pos.x, pos.y };
				}
				if (const auto calendar = RE::Calendar::GetSingleton()) {
					data["hour"] = calendar->GetHour();
					data["days_passed"] = calendar->GetDaysPassed();
				}
				const float maxHealth = player->GetActorValueMax(RE::ActorValue::kHealth);
				if (maxHealth > 0.0f) {
					const float health = player->AsActorValueOwner()->GetActorValue(RE::ActorValue::kHealth);
					data["health"] = std::clamp(health / maxHealth, 0.0f, 1.0f);
				}
				data["in_combat"] = inCombat;
				data["sneaking"] = player->IsSneaking();
				data["paused"] = paused;  // кадр игры в это время — меню или загрузка, не мир (зрение Гослинга)
				// Чья игра — и в status: после перезапуска сервиса game_loaded не повторяется.
				data["character"] = CharacterId();
				data["player_name"] = Name(player->GetActorBase());
				data["level"] = player->GetLevel();

				json people = json::array();
				json threats = json::array();
				for (const auto& entry : around.nearby) {
					if (!Name(entry.actor).empty()) {
						people.push_back(ActorJson(player, entry));
					}
				}
				for (const auto& entry : around.threats) {
					if (!Name(entry.actor).empty()) {
						threats.push_back(ActorJson(player, entry));
					}
				}
				data["nearby"] = std::move(people);
				data["threats"] = std::move(threats);
				if (!partnerNow.is_null()) {
					data["partner"] = partnerNow;
				}
				if (auto gosling = voice::Status(player); !gosling.is_null()) {
					GoslingView(player, gosling);
					data["gosling"] = std::move(gosling);
				}

				Post("status", std::move(data));
				lastStatus = now;
				forceStatus = false;
			}
			last = { true, inCombat, paused, location, cellName };
		}

		void StartTimer()
		{
			std::thread([] {
				for (;;) {
					std::this_thread::sleep_for(kTick);
					SKSE::GetTaskInterface()->AddTask([] { Tick(); });
				}
			}).detach();
		}

		class Sinks :
			public RE::BSTEventSink<RE::TESDeathEvent>,
			public RE::BSTEventSink<RE::TESActorLocationChangeEvent>,
			public RE::BSTEventSink<RE::TESContainerChangedEvent>,
			public RE::BSTEventSink<RE::MenuOpenCloseEvent>,
			public RE::BSTEventSink<RE::LocationDiscovery::Event>,
			public RE::BSTEventSink<RE::TESQuestStageEvent>,
			public RE::BSTEventSink<RE::ObjectiveState::Event>,
			public RE::BSTEventSink<RE::TESEquipEvent>,
			public RE::BSTEventSink<RE::TESTopicInfoEvent>,
			public RE::BSTEventSink<RE::TESTrackedStatsEvent>,
			public RE::BSTEventSink<RE::TESActivateEvent>,
			public RE::BSTEventSink<RE::TESLockChangedEvent>,
			public RE::BSTEventSink<RE::TESSpellCastEvent>
		{
		public:
			static Sinks* GetSingleton()
			{
				static Sinks singleton;
				return &singleton;
			}

			RE::BSEventNotifyControl ProcessEvent(const RE::TESDeathEvent* a_event,
				RE::BSTEventSource<RE::TESDeathEvent>*) override
			{
				const auto player = RE::PlayerCharacter::GetSingleton();
				if (!a_event || !a_event->dead || !a_event->actorDying || !player) {
					return RE::BSEventNotifyControl::kContinue;
				}
				const auto dying = a_event->actorDying.get();
				if (dying == player) {
					// Владос погиб: кто убил (нет убийцы — упал, утонул, ловушка). Гослинг вспомнит после загрузки.
					const auto killer = a_event->actorKiller ? a_event->actorKiller.get() : nullptr;
					const auto actor = killer ? killer->As<RE::Actor>() : nullptr;
					logs::info("Игрок погиб: {}", killer ? Name(killer) : std::string{ "без убийцы" });
					Post("player_died", { { "killer", Name(killer) },
						{ "race", RaceOf(actor) }, { "unique", IsUnique(actor) } });
					return RE::BSEventNotifyControl::kContinue;
				}
				const bool byPlayer = a_event->actorKiller && a_event->actorKiller.get() == player;
				if (dying != player && (byPlayer || player->GetDistance(dying) < kNearbyRadius)) {
					const auto actor = dying->As<RE::Actor>();
					Post("actor_killed", { { "name", Name(dying) }, { "by_player", byPlayer }, { "unique", IsUnique(actor) },
						{ "race", RaceOf(actor) } });
				}
				return RE::BSEventNotifyControl::kContinue;
			}

			RE::BSEventNotifyControl ProcessEvent(const RE::TESActorLocationChangeEvent* a_event,
				RE::BSTEventSource<RE::TESActorLocationChangeEvent>*) override
			{
				if (a_event && a_event->actor && a_event->actor->IsPlayerRef()) {
					forceStatus = true;  // новая локация уйдёт со следующим тиком
				}
				return RE::BSEventNotifyControl::kContinue;
			}

			RE::BSEventNotifyControl ProcessEvent(const RE::TESContainerChangedEvent* a_event,
				RE::BSTEventSource<RE::TESContainerChangedEvent>*) override
			{
				if (a_event && (a_event->newContainer == kPlayerRef || a_event->oldContainer == kPlayerRef)) {
					playerDirty = true;
				}
				// Игрок отдал Гослингу или забрал у него (меню обмена со спутником, «отдать»).
				if (inGame && a_event && a_event->baseObj && a_event->itemCount > 0) {
					if (a_event->oldContainer == kPlayerRef && IsGoslingId(a_event->newContainer)) {
						exchange.given[a_event->baseObj] += a_event->itemCount;
						exchange.lastAt = Clock::now();
					} else if (a_event->newContainer == kPlayerRef && IsGoslingId(a_event->oldContainer)) {
						(Clock::now() < handoverUntil ? exchange.handed : exchange.taken)[a_event->baseObj] += a_event->itemCount;
						exchange.lastAt = Clock::now();
					}
				}
				// Событие для мозга — только подобранное с земли (не из сундуков, не покупки).
				if (inGame && a_event && a_event->newContainer == kPlayerRef && a_event->oldContainer == 0 &&
					a_event->reference) {
					const auto item = RE::TESForm::LookupByID(a_event->baseObj);
					if (auto name = Name(item); !name.empty()) {
						Post("item_picked", { { "name", std::move(name) }, { "count", a_event->itemCount } });
					}
				}
				return RE::BSEventNotifyControl::kContinue;
			}

			RE::BSEventNotifyControl ProcessEvent(const RE::MenuOpenCloseEvent* a_event,
				RE::BSTEventSource<RE::MenuOpenCloseEvent>*) override
			{
				if (a_event && a_event->opening && a_event->menuName == RE::MainMenu::MENU_NAME) {
					inGame = false;
				}
				// Меню взлома: замок, открывшийся при нём (или сразу после), взломан, а не открыт ключом.
				if (a_event && a_event->menuName == RE::LockpickingMenu::MENU_NAME) {
					lockpicking = a_event->opening;
					if (!a_event->opening) {
						lockpickClosed = Clock::now();
					}
				}
				// Игрок открыл книгу (из сумки или с полки): какую — из глобальной переменной меню книги (чтение поля
				// по Address Library, не вызов функции). Текст сервис возьмёт из файлов игры.
				if (inGame && a_event && a_event->opening && a_event->menuName == RE::BookMenu::MENU_NAME) {
					const auto book = RE::BookMenu::GetTargetForm();
					const auto file = book ? book->GetFile(0) : nullptr;
					logs::info("Книга открыта: {}", book ? Name(book) : std::string{ "не узнать" });
					if (book && file && book->GetFormType() == RE::FormType::Book) {
						Post("book_read", { { "name", Name(book) }, { "file", std::string(file->GetFilename()) },
							{ "form", book->GetLocalFormID() } });
					}
				}
				// Пока игрок говорит с NPC, Гослинг не встревает — сервис ждёт конца разговора.
				if (a_event && a_event->menuName == RE::DialogueMenu::MENU_NAME && a_event->opening != inDialogue) {
					inDialogue = a_event->opening;
					Post("dialogue_menu", { { "open", inDialogue } });
				}
				return RE::BSEventNotifyControl::kContinue;
			}

			// Игровая статистика («Locks Picked», «Items Stolen», …): значение — накопленное за всю игру.
			// Из неё сервис выводит привычки Владоса по делам.
			RE::BSEventNotifyControl ProcessEvent(const RE::TESTrackedStatsEvent* a_event,
				RE::BSTEventSource<RE::TESTrackedStatsEvent>*) override
			{
				if (inGame && a_event && !a_event->stat.empty()) {
					Post("stat", { { "name", std::string(a_event->stat.c_str()) }, { "value", a_event->value } });
				}
				return RE::BSEventNotifyControl::kContinue;
			}

			// NPC рядом начал реплику: кто и какая (файл + номер записи INFO). Текст сервис берёт сам из файлов
			// игры — так не нужны ни субтитры, ни функции игры по адресу. Бой и «обнаружение» (крики, «кто тут?»)
			// не шлём: в бою Гослинг и так молчит.
			RE::BSEventNotifyControl ProcessEvent(const RE::TESTopicInfoEvent* a_event,
				RE::BSTEventSource<RE::TESTopicInfoEvent>*) override
			{
				using Type = RE::DIALOGUE_TYPE;
				const auto player = RE::PlayerCharacter::GetSingleton();
				if (!inGame || !a_event || a_event->type != RE::TESTopicInfoEvent::TopicInfoEventType::kTopicBegin ||
					!player) {
					return RE::BSEventNotifyControl::kContinue;
				}
				const auto speaker = a_event->speakerRef.get();
				if (!speaker || speaker == player || voice::IsGosling(speaker) ||
					player->GetDistance(speaker) > kHearRadius) {
					return RE::BSEventNotifyControl::kContinue;
				}
				const auto info = RE::TESForm::LookupByID<RE::TESTopicInfo>(a_event->topicInfoFormID);
				const auto topic = info ? info->parentTopic : nullptr;
				const auto file = info ? info->GetFile(0) : nullptr;
				if (!topic || !file || topic->data.type == Type::kCombat || topic->data.type == Type::kDetection) {
					return RE::BSEventNotifyControl::kContinue;
				}
				// Говорит с игроком (меню разговора открыто) — собеседник, даже если разговор начал сам NPC
				// (сцена, подход к игроку): тогда активации NPC не было. 5 минут считаем от последней реплики.
				if (const auto actor = speaker->As<RE::Actor>(); inDialogue && actor && !actor->IsPlayerTeammate()) {
					if (partner.id != speaker->GetFormID()) {
						partner = { speaker->GetFormID(), Name(speaker) };
						logs::info("Собеседник: {}", partner.name);
					}
					partner.since = Clock::now();
				}
				Post("npc_line", { { "speaker", Name(speaker) },
					{ "file", std::string(file->GetFilename()) },
					{ "form", info->GetLocalFormID() },
					{ "category", static_cast<int>(topic->data.type.get()) },
					{ "subtype", static_cast<int>(topic->data.subtype.get()) },
					{ "to_player", inDialogue } });
				return RE::BSEventNotifyControl::kContinue;
			}

			// Игрок что-то активировал: рычаг, цепь, столб загадки (activator), сундук, книгу, верстак, тело
			// (обыскал), живого NPC (заговорил — он становится собеседником). Двери не шлём: их и так видно
			// по смене помещения, а запертую ключом — по TESLockChangedEvent. Подбор вещей — item_picked.
			RE::BSEventNotifyControl ProcessEvent(const RE::TESActivateEvent* a_event,
				RE::BSTEventSource<RE::TESActivateEvent>*) override
			{
				const auto player = RE::PlayerCharacter::GetSingleton();
				const auto ref = a_event ? a_event->objectActivated.get() : nullptr;
				if (!inGame || !ref || !player || a_event->actionRef.get() != player) {
					return RE::BSEventNotifyControl::kContinue;
				}
				const auto base = ref->GetBaseObject();
				const auto now = Clock::now();
				if (pendingLock == ref->GetFormID() && now - pendingLockAt < kActivateRepeat) {
					pendingLock = 0;
					PostUnlocked(ref);  // замок открылся чуть раньше, чем пришла активация
				}
				const bool repeat = ref->GetFormID() == lastActivated && now - lastActivatedAt < kActivateRepeat;
				lastActivated = ref->GetFormID();
				lastActivatedAt = now;
				if (!base || repeat) {
					return RE::BSEventNotifyControl::kContinue;
				}
				std::string_view kind;
				bool             unique = false;
				if (const auto actor = ref->As<RE::Actor>()) {
					if (voice::IsGosling(actor) || actor->IsPlayerTeammate()) {
						return RE::BSEventNotifyControl::kContinue;
					}
					unique = IsUnique(actor);
					if (actor->IsDead()) {
						kind = "body";
					} else {
						kind = "npc";
						partner = { ref->GetFormID(), Name(ref), now };
						logs::info("Собеседник: {}", partner.name);
					}
				} else {
					switch (base->GetFormType()) {
					case RE::FormType::Activator:
						kind = "activator";
						break;
					case RE::FormType::Container:
						kind = "container";
						break;
					case RE::FormType::Book:  // читает — придёт book_read из меню книги; второе событие не нужно
						return RE::BSEventNotifyControl::kContinue;
					case RE::FormType::Furniture:
						kind = "furniture";
						break;
					default:
						return RE::BSEventNotifyControl::kContinue;
					}
				}
				if (auto name = Name(ref); !name.empty()) {
					Post("activated", { { "name", std::move(name) }, { "kind", kind }, { "unique", unique } });
				}
				return RE::BSEventNotifyControl::kContinue;
			}

			// Замок рядом с игроком открылся без взлома — ключом. Засчитываем, только если игрок сам активировал этот
			// предмет (события приходят в любом порядке — ждём пару): иначе замок открыл скрипт. Двери-переходы
			// (городские ворота) не берём: их игра отпирает сама. Сам замок не читаем: GetLock — функция по адресу.
			RE::BSEventNotifyControl ProcessEvent(const RE::TESLockChangedEvent* a_event,
				RE::BSTEventSource<RE::TESLockChangedEvent>*) override
			{
				const auto player = RE::PlayerCharacter::GetSingleton();
				const auto ref = a_event ? a_event->lockedObject.get() : nullptr;
				if (!inGame || !ref || !player || player->GetDistance(ref) > kTouchRadius) {
					return RE::BSEventNotifyControl::kContinue;
				}
				const auto now = Clock::now();
				const bool picked = lockpicking || now - lockpickClosed < kLockpickAfter;
				const bool loadDoor = ref->extraList.HasType(RE::ExtraDataType::kTeleport);
				logs::info("Замок сменил состояние: {}{}{}", Name(ref), loadDoor ? " (дверь-переход)" : "",
					picked ? ", взлом" : "");
				if (picked || loadDoor) {
					return RE::BSEventNotifyControl::kContinue;
				}
				if (ref->GetFormID() == lastActivated && now - lastActivatedAt < kActivateRepeat) {
					PostUnlocked(ref);
				} else {
					pendingLock = ref->GetFormID();  // активация придёт следом — тогда и засчитаем
					pendingLockAt = now;
				}
				return RE::BSEventNotifyControl::kContinue;
			}

			static void PostUnlocked(RE::TESObjectREFR* a_ref)
			{
				const auto base = a_ref->GetBaseObject();
				Post("unlocked", { { "name", Name(a_ref) }, { "door", base && base->GetFormType() == RE::FormType::Door } });
			}

			// Игрок применил Крик: заклинание типа «голос». Название — у выбранного крика (поле selectedPower),
			// иначе у заклинания. Один крик — одно событие (слова крика могут прийти отдельными заклинаниями).
			RE::BSEventNotifyControl ProcessEvent(const RE::TESSpellCastEvent* a_event,
				RE::BSTEventSource<RE::TESSpellCastEvent>*) override
			{
				const auto player = RE::PlayerCharacter::GetSingleton();
				if (!inGame || !a_event || !player || a_event->object.get() != player) {
					return RE::BSEventNotifyControl::kContinue;
				}
				const auto spell = RE::TESForm::LookupByID<RE::SpellItem>(a_event->spell);
				if (!spell || spell->data.spellType != RE::MagicSystem::SpellType::kVoicePower) {
					return RE::BSEventNotifyControl::kContinue;
				}
				const auto power = player->GetActorRuntimeData().selectedPower;
				auto       name = power && power->GetFormType() == RE::FormType::Shout ? Name(power) : Name(spell);
				const auto now = Clock::now();
				if (name.empty() || (name == lastShout && now - lastShoutAt < kActivateRepeat)) {
					return RE::BSEventNotifyControl::kContinue;
				}
				lastShout = name;
				lastShoutAt = now;
				logs::info("Крик: {} (заклинание {})", name, Name(spell));
				Post("shout", { { "name", std::move(name) } });
				return RE::BSEventNotifyControl::kContinue;
			}

			RE::BSEventNotifyControl ProcessEvent(const RE::LocationDiscovery::Event*,
				RE::BSTEventSource<RE::LocationDiscovery::Event>*) override
			{
				markersDirty = true;  // открыто новое место — со следующим тиком разошлём карту заново
				return RE::BSEventNotifyControl::kContinue;
			}

			RE::BSEventNotifyControl ProcessEvent(const RE::TESQuestStageEvent*,
				RE::BSTEventSource<RE::TESQuestStageEvent>*) override
			{
				questsDirty = true;
				profileDirty = true;  // квест мог завершиться, а с ним — вступление в гильдию, новый ранг
				return RE::BSEventNotifyControl::kContinue;
			}

			RE::BSEventNotifyControl ProcessEvent(const RE::ObjectiveState::Event*,
				RE::BSTEventSource<RE::ObjectiveState::Event>*) override
			{
				questsDirty = true;
				return RE::BSEventNotifyControl::kContinue;
			}

			RE::BSEventNotifyControl ProcessEvent(const RE::TESEquipEvent* a_event,
				RE::BSTEventSource<RE::TESEquipEvent>*) override
			{
				if (a_event && a_event->actor && a_event->actor->IsPlayerRef()) {
					playerDirty = true;
				}
				return RE::BSEventNotifyControl::kContinue;
			}
		};

		void Register()
		{
			const auto sinks = Sinks::GetSingleton();
			const auto holder = RE::ScriptEventSourceHolder::GetSingleton();
			holder->AddEventSink<RE::TESDeathEvent>(sinks);
			holder->AddEventSink<RE::TESActorLocationChangeEvent>(sinks);
			holder->AddEventSink<RE::TESContainerChangedEvent>(sinks);
			holder->AddEventSink<RE::TESQuestStageEvent>(sinks);
			holder->AddEventSink<RE::TESEquipEvent>(sinks);
			holder->AddEventSink<RE::TESTopicInfoEvent>(sinks);
			holder->AddEventSink<RE::TESTrackedStatsEvent>(sinks);
			holder->AddEventSink<RE::TESActivateEvent>(sinks);
			holder->AddEventSink<RE::TESLockChangedEvent>(sinks);
			holder->AddEventSink<RE::TESSpellCastEvent>(sinks);
			RE::ObjectiveState::GetEventSource()->AddEventSink(sinks);
			RE::UI::GetSingleton()->AddEventSink<RE::MenuOpenCloseEvent>(sinks);
			RE::LocationDiscovery::GetEventSource()->AddEventSink(sinks);
			StartTimer();
			logs::info("События подключены, сервис: 127.0.0.1:{}", net::kPort);
		}

		void OnGameLoaded()
		{
			inGame = true;
			last = {};
			forceStatus = true;
			markersDirty = true;
			questsDirty = true;
			playerDirty = true;
			profileDirty = true;
			const auto player = RE::PlayerCharacter::GetSingleton();
			const auto manager = RE::BGSSaveLoadManager::GetSingleton();
			logs::info("Персонаж: из сейва '{}', поле currentCharacterID {:08X}, дней прошло {:.2f}", saveCharacter,
				manager ? manager->currentCharacterID : 0, DaysPassed());
			Post("game_loaded", { { "character", CharacterId() },
				{ "player_name", player ? Name(player->GetActorBase()) : std::string{} },
				{ "days_passed", DaysPassed() } });
			voice::LogQuestState();
			orders::StopTravel();
		}

		// «Autosave1_02CCEDA2_1_…» -> «02CCEDA2»: второе поле имени сейва — ID персонажа.
		std::string CharacterFromSave(std::string_view a_name)
		{
			if (const auto slash = a_name.find_last_of("\\/"); slash != std::string_view::npos) {
				a_name.remove_prefix(slash + 1);  // на случай, если пришёл путь, а не имя
			}
			const auto first = a_name.find('_');
			const auto second = first == std::string_view::npos ? first : a_name.find('_', first + 1);
			if (second == std::string_view::npos || second - first != 9) {
				return {};
			}
			const auto id = a_name.substr(first + 1, 8);
			return std::ranges::all_of(id, [](char c) { return std::isxdigit(static_cast<unsigned char>(c)); }) ?
			           std::string(id) :
			           std::string{};
		}
	}

	void ExpectHandover()
	{
		handoverUntil = Clock::now() + kHandoverFor;
	}

	void OnMessage(SKSE::MessagingInterface::Message* a_msg)
	{
		switch (a_msg->type) {
		case SKSE::MessagingInterface::kDataLoaded:
			Register();
			break;
		case SKSE::MessagingInterface::kPreLoadGame:
			saveCharacter = a_msg->data ?
			                    CharacterFromSave({ static_cast<const char*>(a_msg->data), a_msg->dataLen }) :
			                    std::string{};
			break;
		case SKSE::MessagingInterface::kNewGame:
			saveCharacter.clear();  // новый персонаж: ID появится в поле игры
			OnGameLoaded();
			break;
		case SKSE::MessagingInterface::kPostLoadGame:
			OnGameLoaded();
			break;
		default:
			break;
		}
	}
}
