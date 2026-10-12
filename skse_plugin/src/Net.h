#pragma once

// Отправка событий в сервис компаньона. HTTP идёт в отдельном потоке: игровой поток не ждёт сеть.
namespace net
{
	inline constexpr std::uint16_t kPort = 8765;

	void Start();

	// {"type": ..., "data": {...}} -> POST /game/event. Потокобезопасно, не блокирует.
	void Post(const json& a_event);

	// Команды от сервиса (сказать реплику, позже — действия): отдельный поток держит
	// «долгий» GET /game/commands; каждую команду отдаёт в a_handler (из этого потока, не игрового).
	void StartCommands(std::function<void(const json&)> a_handler);

	// true один раз после того, как сервис снова ответил (был перезапущен) — пора переслать ему карту.
	bool TakeReconnected();

	// Строки игры -> UTF-8 (русские строки Скайрима бывают в cp1251).
	std::string ToUtf8(const char* a_text);
}
