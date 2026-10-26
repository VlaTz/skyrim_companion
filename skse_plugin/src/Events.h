#pragma once

// События игры -> сервис. Формат — companion/game/state.py:
//   status         — снимок состояния: раз в kStatusEvery и сразу при смене места или боя
//   game_loaded, combat_started, combat_ended, actor_killed, item_picked — разовые события
namespace events
{
	void OnMessage(SKSE::MessagingInterface::Message* a_msg);

	// Гослинг сейчас отдаст игроку находки (приказ give): что придёт от него — «отдал сам» (handed), а не «забрал».
	void ExpectHandover();
}
