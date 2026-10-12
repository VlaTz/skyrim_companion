#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <Windows.h>
#include <winhttp.h>

#include "Net.h"

namespace net
{
	namespace
	{
		constexpr std::size_t kMaxQueue = 200;  // сервис не запущен — старые события выбрасываем

		std::mutex              mutex;
		std::condition_variable wake;
		std::deque<std::string> queue;
		std::atomic_bool        reconnected = false;

		bool Send(HINTERNET a_connect, const std::string& a_body)
		{
			HINTERNET request = WinHttpOpenRequest(a_connect, L"POST", L"/game/event", nullptr,
				WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
			if (!request) {
				return false;
			}
			constexpr wchar_t headers[] = L"Content-Type: application/json; charset=utf-8";
			const auto        size = static_cast<DWORD>(a_body.size());
			bool              ok = WinHttpSendRequest(request, headers, static_cast<DWORD>(-1),
							   const_cast<char*>(a_body.data()), size, size, 0) &&
			          WinHttpReceiveResponse(request, nullptr);
			if (ok) {
				DWORD status = 0;
				DWORD length = sizeof(status);
				WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
					WINHTTP_HEADER_NAME_BY_INDEX, &status, &length, WINHTTP_NO_HEADER_INDEX);
				if (status != 200) {
					logs::warn("Сервис ответил {} на {}", status, a_body);
				}
			}
			WinHttpCloseHandle(request);
			return ok;
		}

		void Worker()
		{
			HINTERNET session = WinHttpOpen(L"SkyrimCompanion", WINHTTP_ACCESS_TYPE_NO_PROXY,
				WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
			WinHttpSetTimeouts(session, 1000, 1000, 2000, 3000);  // resolve, connect, send, receive (мс)
			HINTERNET connect = WinHttpConnect(session, L"127.0.0.1", kPort, 0);

			bool online = true;
			for (;;) {
				std::string body;
				{
					std::unique_lock lock(mutex);
					wake.wait(lock, [] { return !queue.empty(); });
					body = std::move(queue.front());
					queue.pop_front();
				}
				const bool ok = Send(connect, body);
				if (ok != online) {
					online = ok;
					if (ok) {
						reconnected = true;
						logs::info("Сервис компаньона снова на связи");
					} else {
						logs::warn("Сервис компаньона не отвечает на 127.0.0.1:{} — события теряются, пока он не запущен", kPort);
					}
				}
			}
		}
	}

	void Start()
	{
		std::thread(Worker).detach();
	}

	void StartCommands(std::function<void(const json&)> a_handler)
	{
		std::thread([handler = std::move(a_handler)] {
			HINTERNET session = WinHttpOpen(L"SkyrimCompanion", WINHTTP_ACCESS_TYPE_NO_PROXY,
				WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
			// Сервис держит запрос до 20 с, пока не появится команда — ждём ответа с запасом.
			WinHttpSetTimeouts(session, 1000, 1000, 2000, 30000);
			HINTERNET connect = WinHttpConnect(session, L"127.0.0.1", kPort, 0);
			for (;;) {
				std::string body;
				bool        ok = false;
				if (HINTERNET request = WinHttpOpenRequest(connect, L"GET", L"/game/commands?wait=20", nullptr,
						WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, 0)) {
					ok = WinHttpSendRequest(request, WINHTTP_NO_ADDITIONAL_HEADERS, 0, WINHTTP_NO_REQUEST_DATA, 0, 0, 0) &&
					     WinHttpReceiveResponse(request, nullptr);
					DWORD available = 0;
					while (ok && WinHttpQueryDataAvailable(request, &available) && available > 0) {
						std::string chunk(available, '\0');
						DWORD       read = 0;
						if (!WinHttpReadData(request, chunk.data(), available, &read)) {
							ok = false;
							break;
						}
						body.append(chunk.data(), read);
					}
					WinHttpCloseHandle(request);
				}
				if (!ok) {  // сервис не запущен — пробуем снова чуть позже
					std::this_thread::sleep_for(std::chrono::seconds(2));
					continue;
				}
				const auto reply = json::parse(body, nullptr, false);
				if (reply.is_object() && reply.contains("commands") && reply["commands"].is_array()) {
					for (const auto& command : reply["commands"]) {
						handler(command);
					}
				}
			}
		}).detach();
	}

	void Post(const json& a_event)
	{
		auto body = a_event.dump(-1, ' ', false, json::error_handler_t::replace);
		{
			std::lock_guard lock(mutex);
			if (queue.size() >= kMaxQueue) {
				queue.pop_front();
			}
			queue.push_back(std::move(body));
		}
		wake.notify_one();
	}

	bool TakeReconnected()
	{
		return reconnected.exchange(false);
	}

	std::string ToUtf8(const char* a_text)
	{
		if (!a_text || !*a_text) {
			return {};
		}
		const auto len = static_cast<int>(std::strlen(a_text));
		if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, a_text, len, nullptr, 0) > 0) {
			return a_text;
		}
		constexpr UINT cp1251 = 1251;
		const int      wlen = MultiByteToWideChar(cp1251, 0, a_text, len, nullptr, 0);
		std::wstring   wide(wlen, L'\0');
		MultiByteToWideChar(cp1251, 0, a_text, len, wide.data(), wlen);
		const int   ulen = WideCharToMultiByte(CP_UTF8, 0, wide.data(), wlen, nullptr, 0, nullptr, nullptr);
		std::string out(ulen, '\0');
		WideCharToMultiByte(CP_UTF8, 0, wide.data(), wlen, out.data(), ulen, nullptr, nullptr);
		return out;
	}
}
