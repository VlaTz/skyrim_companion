#pragma once

// Подробности об игроке для инструмента player_details: золото, что надето, что в сумке,
// активные эффекты. Только чтение данных в памяти — функций игры по адресу не вызываем.
namespace player
{
	json Collect(RE::PlayerCharacter* a_player);
}
