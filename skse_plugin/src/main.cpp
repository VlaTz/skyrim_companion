#include "Events.h"
#include "Net.h"
#include "Voice.h"

SKSE_PLUGIN_LOAD(const SKSE::LoadInterface* a_skse)
{
	SKSE::Init(a_skse);

	net::Start();
	net::StartCommands(voice::HandleCommand);
	SKSE::GetPapyrusInterface()->Register(voice::RegisterPapyrus);
	SKSE::GetMessagingInterface()->RegisterListener(events::OnMessage);

	logs::info("Skyrim Companion загружен");
	return true;
}
