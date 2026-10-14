#include "Voice.h"

#include "Net.h"

namespace voice
{
	namespace
	{
		// Номера форм в Gosling.esp — как в companion/gosling_mod.py.
		constexpr std::string_view          kPlugin = "Gosling.esp";
		constexpr RE::FormID                kNpcRef = 0x802;
		constexpr RE::FormID                kVoiceQuest = 0x803;
		constexpr RE::FormID                kVoiceType = 0x814;
		constexpr std::array<RE::FormID, 2> kTopics = { 0x810, 0x812 };
		constexpr float                     kUnitsPerMeter = 70.0f;
		constexpr double                    kStaleCommand = 2.0;  // с в очереди игрового потока — игра была на паузе

		RE::Actor* Gosling()
		{
			const auto handler = RE::TESDataHandler::GetSingleton();
			return handler ? handler->LookupForm<RE::Actor>(kNpcRef, kPlugin) : nullptr;
		}

		// Сколько ждать, что реплика зазвучала, и как часто проверять. Say() идёт через Papyrus (не сразу),
		// звук стартует ещё чуть позже (~0,13 с). Сразу после прошлой фразы игра Say() молча отбрасывает
		// (Гослинг для неё ещё «говорит») — тогда повторяем Say(), пока не зазвучит.
		constexpr auto kStartTimeout = std::chrono::milliseconds(10000);
		constexpr auto kStartPoll = std::chrono::milliseconds(100);
		constexpr auto kSayRetry = std::chrono::milliseconds(600);

		using SoundIds = std::array<std::uint32_t, 2>;

		// Звуки голоса актёра — дескрипторы в его процессе. Только читаем поля: функции звука
		// (BSSoundHandle::IsPlaying и т. п.) — по адресу, на 1.7.x риск.
		SoundIds VoiceSounds(RE::Actor* a_actor)
		{
			SoundIds   ids{ RE::BSSoundHandle::kInvalidID, RE::BSSoundHandle::kInvalidID };
			const auto process = a_actor ? a_actor->GetActorRuntimeData().currentProcess : nullptr;
			if (const auto high = process ? process->high : nullptr) {
				for (std::size_t i = 0; i < ids.size(); ++i) {
					ids[i] = high->soundHandles[i].soundID;
				}
			}
			return ids;
		}

		// Сколько ещё игра считает актёра говорящим (для лога: подобрать паузу между фразами).
		float VoiceTimer(RE::Actor* a_actor)
		{
			const auto process = a_actor ? a_actor->GetActorRuntimeData().currentProcess : nullptr;
			const auto high = process ? process->high : nullptr;
			return high ? high->voiceTimer : -1.0f;
		}

		// Субтитр нашей реплики — «...» (текст заглушки в Gosling.esp). Подменить текст в SubtitleManager мало:
		// HUD берёт текст, когда субтитр появился, и дальше туда не смотрит (проверено в игре 2026-10-14).
		// Поэтому, пока у Гослинга есть запись в SubtitleManager (он говорит), сами показываем текст в HUD —
		// функцией ShowSubtitle из hudmenu.swf (Scaleform Invoke — виртуальный метод, не функция по адресу).
		// Запись в менеджере тоже правим: если игра перечитает субтитр, возьмёт наш текст.
		constexpr auto kSubtitlePoll = std::chrono::milliseconds(100);
		constexpr auto kSubtitleTail = std::chrono::milliseconds(500);  // запас к длине фразы

		struct Subtitle
		{
			std::string                           text;     // фраза
			std::string                           shown;    // как в HUD: «Гослинг: фраза»
			std::chrono::steady_clock::time_point until;
			bool                                  seen = false;  // запись Гослинга в менеджере уже появлялась
		};

		std::string EscapeHtml(std::string_view a_text)
		{
			std::string out;
			for (const char c : a_text) {
				switch (c) {
				case '&': out += "&amp;"; break;
				case '<': out += "&lt;"; break;
				case '>': out += "&gt;"; break;
				default: out += c;
				}
			}
			return out;
		}

		void ShowInHud(std::string a_text)
		{
			SKSE::GetTaskInterface()->AddUITask([text = std::move(a_text)] {
				const auto ui = RE::UI::GetSingleton();
				const auto hud = ui ? ui->GetMenu(RE::HUDMenu::MENU_NAME) : nullptr;
				if (hud && hud->uiMovie) {
					const RE::GFxValue arg(text.c_str());
					hud->uiMovie->Invoke("_root.HUDMovieBaseInstance.ShowSubtitle", nullptr, &arg, 1);
				}
			});
		}

		// Игровой поток. Поле subtitle записей SubtitleManager — под его же блокировкой.
		void PatchSubtitle(std::shared_ptr<Subtitle> a_subtitle)
		{
			const auto actor = Gosling();
			const auto manager = RE::SubtitleManager::GetSingleton();
			bool       speaking = false;
			if (actor && manager) {
				const auto          handle = actor->GetHandle();
				RE::BSSpinLockGuard guard(manager->lock);
				for (auto& info : manager->subtitles) {
					if (info.speaker == handle) {
						speaking = true;
						if (std::string_view(info.subtitle) != a_subtitle->text) {
							info.subtitle = a_subtitle->text.c_str();
						}
					}
				}
			}
			if (speaking) {
				if (!a_subtitle->seen) {
					logs::info("Субтитр Гослинга: «{}»", a_subtitle->text);
				}
				a_subtitle->seen = true;
				ShowInHud(a_subtitle->shown);
			} else if (a_subtitle->seen) {
				return;  // реплика кончилась — субтитр убирает сама игра; не воскрешаем его
			}
			if (std::chrono::steady_clock::now() < a_subtitle->until) {
				std::thread([a_subtitle = std::move(a_subtitle)]() mutable {
					std::this_thread::sleep_for(kSubtitlePoll);
					SKSE::GetTaskInterface()->AddTask([a_subtitle = std::move(a_subtitle)] { PatchSubtitle(a_subtitle); });
				}).detach();
			} else if (!a_subtitle->seen) {
				logs::info("Субтитр Гослинга не появился (субтитры выключены в настройках?)");
			}
		}

		// Реплика, которую сказали, но ещё не знаем, зазвучала ли.
		struct PendingLine
		{
			std::size_t                           line;
			std::string                           id;
			std::string                           subtitle;  // текст фразы для субтитра
			double                                duration;  // длина фразы, с
			SoundIds                              before;  // звуки голоса до Say()
			std::chrono::steady_clock::time_point started;
			std::chrono::steady_clock::time_point lastSay;
			int                                   attempts = 0;
		};

		// GoslingVoice.SayLine(Гослинг, тема) через Papyrus. Пустая строка — вызвали, иначе причина отказа.
		std::string DispatchSay(PendingLine& a_line)
		{
			const auto actor = Gosling();
			const auto handler = RE::TESDataHandler::GetSingleton();
			const auto topic = a_line.line < kTopics.size() ? handler->LookupForm<RE::TESTopic>(kTopics[a_line.line], kPlugin) : nullptr;
			if (!actor || !topic) {
				return "нет Gosling.esp или его тем";
			}
			if (actor->IsDisabled() || !actor->Is3DLoaded()) {
				return "Гослинг не рядом (не загружен)";
			}
			if (a_line.attempts == 0) {
				a_line.before = VoiceSounds(actor);
			}
			const auto vm = RE::BSScript::Internal::VirtualMachine::GetSingleton();
			RE::BSTSmartPointer<RE::BSScript::IStackCallbackFunctor> callback;
			const auto args = RE::MakeFunctionArguments(static_cast<RE::Actor*>(actor), static_cast<RE::TESTopic*>(topic));
			if (!vm || !vm->DispatchStaticCall("GoslingVoice", "SayLine", args, callback)) {
				return "Papyrus: не удалось вызвать GoslingVoice.SayLine";
			}
			logs::info("Реплика {}: Say() №{}, voiceTimer {:.2f}", a_line.id, a_line.attempts + 1, VoiceTimer(actor));
			++a_line.attempts;
			a_line.lastSay = std::chrono::steady_clock::now();
			return {};
		}

		void Report(const PendingLine& a_line, const std::string& a_reason)
		{
			if (!a_reason.empty()) {
				logs::warn("Реплика {} не сказана: {}", a_line.line, a_reason);
			}
			net::Post({ { "type", "say_result" },
				{ "data", { { "id", a_line.id }, { "ok", a_reason.empty() }, { "reason", a_reason } } } });
		}

		void CheckStarted(std::shared_ptr<PendingLine> a_line);

		void WatchStart(std::shared_ptr<PendingLine> a_line)
		{
			std::thread([a_line = std::move(a_line)]() mutable {
				std::this_thread::sleep_for(kStartPoll);
				SKSE::GetTaskInterface()->AddTask([a_line = std::move(a_line)] { CheckStarted(a_line); });
			}).detach();
		}

		// Игровой поток. Зазвучала = у Гослинга появился новый звук голоса после Say(). Нет звука kSayRetry
		// после Say() — повторяем; через kStartTimeout отказ, и сервис договорит фразу через колонки.
		void CheckStarted(std::shared_ptr<PendingLine> a_line)
		{
			const auto actor = Gosling();
			const auto now = VoiceSounds(actor);
			const auto clock = std::chrono::steady_clock::now();
			const auto waited = clock - a_line->started;
			for (std::size_t i = 0; i < now.size(); ++i) {
				if (now[i] != RE::BSSoundHandle::kInvalidID && now[i] != a_line->before[i]) {
					logs::info("Реплика {} зазвучала через {:.2f} с, с {}-й попытки (звук {:08X})", a_line->id,
						std::chrono::duration<double>(waited).count(), a_line->attempts, now[i]);
					Report(*a_line, {});
					if (!a_line->subtitle.empty()) {
						const auto name = actor ? net::ToUtf8(actor->GetDisplayFullName()) : std::string{};
						const auto length = std::chrono::duration_cast<std::chrono::steady_clock::duration>(
							std::chrono::duration<double>(a_line->duration));
						PatchSubtitle(std::make_shared<Subtitle>(a_line->subtitle,
							EscapeHtml(name.empty() ? a_line->subtitle : name + ": " + a_line->subtitle),
							clock + length + kSubtitleTail));
					}
					return;
				}
			}
			if (waited >= kStartTimeout) {
				logs::info("Реплика {}: звуки голоса до {:08X} {:08X}, после {:08X} {:08X}, voiceTimer {:.2f}", a_line->id,
					a_line->before[0], a_line->before[1], now[0], now[1], VoiceTimer(actor));
				Report(*a_line, std::format("реплика не зазвучала (попыток Say(): {})", a_line->attempts));
				return;
			}
			if (clock - a_line->lastSay >= kSayRetry) {
				if (const auto reason = DispatchSay(*a_line); !reason.empty()) {
					Report(*a_line, reason);
					return;
				}
			}
			WatchStart(std::move(a_line));
		}

		void Say(std::size_t a_line, const std::string& a_id, const std::string& a_subtitle, double a_duration)
		{
			const auto now = std::chrono::steady_clock::now();
			auto       pending = std::make_shared<PendingLine>(a_line, a_id, a_subtitle, a_duration, SoundIds{}, now, now);
			if (const auto reason = DispatchSay(*pending); !reason.empty()) {
				Report(*pending, reason);
				return;
			}
			WatchStart(std::move(pending));  // «ok» — только когда звук реально пошёл
		}
	}

	namespace
	{
		// GoslingVoice.Log(string) — скрипт сообщает о своих шагах прямо в наш лог
		// (лог самого Papyrus при запуске через MO2 у нас не включился).
		void PapyrusLog(RE::StaticFunctionTag*, std::string a_message)
		{
			logs::info("[Papyrus] {}", a_message);
		}

		RE::FormID FormIdOf(const RE::TESForm* a_form)
		{
			return a_form ? a_form->GetFormID() : 0;
		}

		// Тип голоса на время реплики (как SetVoiceType у Mantella): Гослингу и его шаблону — GoslingVoice,
		// потом прежний. Шаблон тоже: у Гослинга голос берётся из шаблона (Use Traits).
		// Натив без флага «из тасклетов» — VM выполняет его в игровом потоке, гонок нет.
		std::vector<std::pair<RE::TESNPC*, RE::BGSVoiceType*>> savedVoices;
		int                                                   ownVoiceDepth = 0;

		void UseOwnVoice(RE::StaticFunctionTag*, RE::Actor* a_actor, bool a_own)
		{
			const auto base = a_actor ? a_actor->GetActorBase() : nullptr;
			if (!base) {
				return;
			}
			if (!a_own) {
				if (ownVoiceDepth > 0 && --ownVoiceDepth == 0) {
					for (const auto& [npc, voice] : savedVoices) {
						npc->voiceType = voice;
					}
					savedVoices.clear();
				}
				return;
			}
			const auto own = RE::TESDataHandler::GetSingleton()->LookupForm<RE::BGSVoiceType>(kVoiceType, kPlugin);
			if (!own) {
				logs::warn("Тип голоса GoslingVoice не найден в Gosling.esp");
				return;
			}
			if (ownVoiceDepth++ == 0) {
				const auto before = base->GetObjectVoiceType();
				for (auto npc = base; npc && savedVoices.size() < 4;
					 npc = npc->baseTemplateForm ? npc->baseTemplateForm->As<RE::TESNPC>() : nullptr) {
					savedVoices.emplace_back(npc, npc->voiceType);
					npc->voiceType = own;
				}
				logs::info("Голос Гослинга: свой {:08X}, итоговый {:08X}, шаблон {:08X} -> GoslingVoice {:08X} (итоговый теперь {:08X})",
					FormIdOf(savedVoices.front().second), FormIdOf(before), FormIdOf(base->baseTemplateForm),
					own->GetFormID(), FormIdOf(base->GetObjectVoiceType()));
			}
		}
	}

	bool RegisterPapyrus(RE::BSScript::IVirtualMachine* a_vm)
	{
		a_vm->RegisterFunction("Log", "GoslingVoice", PapyrusLog);
		a_vm->RegisterFunction("UseOwnVoice", "GoslingVoice", UseOwnVoice);
		return true;
	}

	void LogQuestState()
	{
		const auto handler = RE::TESDataHandler::GetSingleton();
		const auto quest = handler ? handler->LookupForm<RE::TESQuest>(kVoiceQuest, kPlugin) : nullptr;
		if (!quest) {
			logs::warn("Квест голоса GoslingVQ не найден — Gosling.esp не подключён?");
			return;
		}
		logs::info("Квест голоса GoslingVQ: флаги {:#06x}, запущен (kEnabled): {}, стартует с игрой: {}",
			quest->data.flags.underlying(), quest->IsEnabled(), quest->StartsEnabled());
	}

	bool IsGosling(const RE::TESObjectREFR* a_ref)
	{
		return a_ref && a_ref == Gosling();
	}

	json Status(RE::PlayerCharacter* a_player)
	{
		const auto actor = Gosling();
		if (!actor) {
			return nullptr;
		}
		const bool loaded = !actor->IsDisabled() && actor->Is3DLoaded();
		return { { "loaded", loaded },
			{ "distance", loaded ? a_player->GetDistance(actor) / kUnitsPerMeter : -1.0f },
			{ "follower", actor->IsPlayerTeammate() } };
	}

	void HandleCommand(const json& a_command)
	{
		if (a_command.value("type", "") != "say") {
			logs::warn("Неизвестная команда: {}", a_command.dump());
			return;
		}
		const auto line = a_command.value("line", 0);
		auto       id = a_command.value("id", "");
		auto       text = a_command.value("text", "");  // фраза — для субтитра
		const auto duration = a_command.value("duration", 3.0);  // её длина, с — столько держим субтитр
		logs::info("Команда say {} (тема {}) получена", id, line);
		const auto received = std::chrono::steady_clock::now();
		SKSE::GetTaskInterface()->AddTask([line, id = std::move(id), text = std::move(text), duration, received] {
			const auto waited = std::chrono::duration<double>(std::chrono::steady_clock::now() - received).count();
			logs::info("Команда say {}: выполняется в игровом потоке (ждала {:.2f} с)", id, waited);
			// Игра стояла на паузе (меню, свёрнута): сервис эту фразу уже пустил или вот-вот пустит в колонки —
			// не говорим её второй раз из Гослинга.
			if (waited > kStaleCommand) {
				net::Post({ { "type", "say_result" },
					{ "data", { { "id", id }, { "ok", false }, { "reason", "игра стояла на паузе" } } } });
				return;
			}
			Say(static_cast<std::size_t>(line), id, text, duration);
		});
	}
}
