#include "Events.h"

#include "Net.h"
#include "Player.h"
#include "Quests.h"
#include "Voice.h"

namespace events
{
	namespace
	{
		using namespace std::chrono_literals;
		using Clock = std::chrono::steady_clock;

		constexpr auto        kTick = 1s;                // как часто проверяем состояние
		constexpr auto        kStatusEvery = 5s;         // снимок status — не реже
		constexpr auto        kPlayerMinGap = 3s;        // подробности об игроке — не чаще (разграбление сундука)
		constexpr auto        kPlayerEvery = 30s;        // и не реже (эффекты меняются без событий)
		constexpr float       kNearbyRadius = 2500.0f;   // ~35 м
		constexpr std::size_t kNearbyMax = 10;
		constexpr float       kHearRadius = 1750.0f;     // ~25 м: реплики NPC дальше Гослинг не слышит
		constexpr RE::FormID  kPlayerRef = 0x14;

		// Всё ниже трогается только из игрового потока (обработчики событий и задачи AddTask).
		struct Last
		{
			bool        valid = false;
			bool        inCombat = false;
			std::string location;
			std::string cell;
		} last;

		Clock::time_point lastStatus{};
		bool              forceStatus = false;
		bool              inGame = false;  // загружена игра, а не главное меню
		bool              inDialogue = false;  // игрок в меню разговора с NPC

		const RE::TESWorldSpace* markersWorld = nullptr;  // для какого мира последний раз слали маркеры карты
		bool                     markersDirty = true;     // открыто новое место — разослать заново
		bool                     questsDirty = true;      // сменился этап или цель квеста — разослать журнал
		bool                     playerDirty = true;      // сменились инвентарь или экипировка
		Clock::time_point        lastPlayer{};

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
		void SendMarkers(RE::TESWorldSpace* a_world)
		{
			json markers = json::array();
			if (const auto cell = a_world->persistentCell) {
				cell->ForEachReference([&](RE::TESObjectREFR* a_ref) {
					const auto extra = a_ref ? a_ref->extraList.GetByType<RE::ExtraMapMarker>() : nullptr;
					const auto data = extra ? extra->mapData : nullptr;
					if (data && data->type.get() < RE::MARKER_TYPE::kTotalLocationTypes) {
						if (auto name = net::ToUtf8(data->locationName.GetFullName()); !name.empty()) {
							const auto pos = a_ref->GetPosition();
							markers.push_back({ { "name", std::move(name) },
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

		std::vector<Near> NearbyActors(RE::PlayerCharacter* a_player)
		{
			std::vector<Near> found;
			RE::ProcessLists::GetSingleton()->ForEachHighActor([&](RE::Actor* a_actor) {
				if (a_actor && a_actor != a_player) {
					const float distance = a_player->GetDistance(a_actor);
					if (distance < kNearbyRadius) {
						found.push_back({ distance, a_actor });
					}
				}
				return RE::BSContainer::ForEachResult::kContinue;
			});
			std::ranges::sort(found, {}, &Near::distance);
			if (found.size() > kNearbyMax) {
				found.resize(kNearbyMax);
			}
			return found;
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
			const auto        nearby = NearbyActors(player);

			if (net::TakeReconnected()) {  // сервис перезапущен — он ничего не знает, шлём всё заново
				markersDirty = true;
				questsDirty = true;
				playerDirty = true;
				forceStatus = true;
			}

			if (questsDirty) {
				questsDirty = false;
				Post("quests", { { "quests", quests::Collect() } });
			}

			if (const auto now = Clock::now();
				(playerDirty && now - lastPlayer >= kPlayerMinGap) || now - lastPlayer >= kPlayerEvery) {
				playerDirty = false;
				lastPlayer = now;
				Post("player", player::Collect(player));
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
					json enemies = json::array();
					for (const auto& [distance, actor] : nearby) {
						if (actor->IsHostileToActor(player) && !actor->IsDead()) {
							if (auto name = Name(actor); !name.empty()) {
								enemies.push_back(std::move(name));
							}
						}
					}
					Post("combat_started", { { "enemies", std::move(enemies) } });
				} else {
					Post("combat_ended");
				}
			}

			const auto now = Clock::now();
			const bool changed = !last.valid || inCombat != last.inCombat ||
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
				}
				const float maxHealth = player->GetActorValueMax(RE::ActorValue::kHealth);
				if (maxHealth > 0.0f) {
					const float health = player->AsActorValueOwner()->GetActorValue(RE::ActorValue::kHealth);
					data["health"] = std::clamp(health / maxHealth, 0.0f, 1.0f);
				}
				data["in_combat"] = inCombat;
				data["sneaking"] = player->IsSneaking();
				data["level"] = player->GetLevel();

				json people = json::array();
				for (const auto& [distance, actor] : nearby) {
					if (auto name = Name(actor); !name.empty()) {
						people.push_back({ { "name", std::move(name) },
							{ "hostile", actor->IsHostileToActor(player) },
							{ "dead", actor->IsDead() },
							{ "follower", actor->IsPlayerTeammate() } });
					}
				}
				data["nearby"] = std::move(people);
				if (auto gosling = voice::Status(player); !gosling.is_null()) {
					data["gosling"] = std::move(gosling);
				}

				Post("status", std::move(data));
				lastStatus = now;
				forceStatus = false;
			}
			last = { true, inCombat, location, cellName };
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
			public RE::BSTEventSink<RE::TESTopicInfoEvent>
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
				const bool byPlayer = a_event->actorKiller && a_event->actorKiller.get() == player;
				if (dying != player && (byPlayer || player->GetDistance(dying) < kNearbyRadius)) {
					Post("actor_killed", { { "name", Name(dying) }, { "by_player", byPlayer } });
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
				// Пока игрок говорит с NPC, Гослинг не встревает — сервис ждёт конца разговора.
				if (a_event && a_event->menuName == RE::DialogueMenu::MENU_NAME && a_event->opening != inDialogue) {
					inDialogue = a_event->opening;
					Post("dialogue_menu", { { "open", inDialogue } });
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
				Post("npc_line", { { "speaker", Name(speaker) },
					{ "file", std::string(file->GetFilename()) },
					{ "form", info->GetLocalFormID() },
					{ "category", static_cast<int>(topic->data.type.get()) },
					{ "subtype", static_cast<int>(topic->data.subtype.get()) },
					{ "to_player", inDialogue } });
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
			Post("game_loaded");
			voice::LogQuestState();
		}
	}

	void OnMessage(SKSE::MessagingInterface::Message* a_msg)
	{
		switch (a_msg->type) {
		case SKSE::MessagingInterface::kDataLoaded:
			Register();
			break;
		case SKSE::MessagingInterface::kPostLoadGame:
		case SKSE::MessagingInterface::kNewGame:
			OnGameLoaded();
			break;
		default:
			break;
		}
	}
}
