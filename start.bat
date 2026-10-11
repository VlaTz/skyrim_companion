@echo off
rem Skyrim companion: Whisper -> LLM -> XTTS. Settings: config.yaml
rem Start your LLM server first (LM Studio / Ollama), see section "llm" in config.yaml
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" -m companion %*
pause
