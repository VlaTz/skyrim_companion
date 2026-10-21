#include "Events.h"
#include "Net.h"
#include "Orders.h"
#include "Voice.h"

SKSE_PLUGIN_LOAD(const SKSE::LoadInterface* a_skse)
{
	SKSE::Init(a_skse);

	net::Start();
	net::StartCommands([](const json& a_command) {
		if (a_command.value("type", "") == "order") {
			orders::HandleCommand(a_command);
		} else {
			voice::HandleCommand(a_command);
		}
	});
	SKSE::GetPapyrusInterface()->Register(voice::RegisterPapyrus);
	SKSE::GetPapyrusInterface()->Register(orders::RegisterPapyrus);
	SKSE::GetMessagingInterface()->RegisterListener(events::OnMessage);

	logs::info("Skyrim Companion загружен");
	return true;
}
