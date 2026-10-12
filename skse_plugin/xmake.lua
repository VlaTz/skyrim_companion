-- SKSE-плагин компаньона: шлёт события игры в сервис (POST http://127.0.0.1:8765/game/event).
-- Сборка:  xmake f --skyrim_vr=n -m releasedbg -y   (один раз)
--          xmake build -y
-- Результат: build/windows/x64/releasedbg/SkyrimCompanion.dll

set_xmakever("3.0.0")

includes("lib/commonlibsse-ng")

set_project("SkyrimCompanion")
set_version("0.1.0")
set_license("GPL-3.0")
set_languages("c++23")
set_warnings("allextra")
set_encodings("utf-8")

add_rules("mode.debug", "mode.releasedbg")
set_defaultmode("releasedbg")

add_requires("nlohmann_json v3.12.0")

target("SkyrimCompanion")
    add_rules("commonlibsse-ng.plugin", {
        name = "SkyrimCompanion",
        author = "vladt",
        description = "AI companion bridge: game events -> companion service"
    })

    add_packages("nlohmann_json")
    add_syslinks("winhttp")

    add_files("src/**.cpp")
    add_headerfiles("src/**.h")
    add_includedirs("src")
    set_pcxxheader("src/pch.h")
