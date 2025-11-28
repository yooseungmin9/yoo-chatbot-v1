<!-- .github/copilot-instructions.md -->
# Repo quick-help for AI coding agents — `yoo-chatbot-v1`

This file gives focused, actionable knowledge to help an AI coding agent be immediately productive in this repository.

1) Big picture
- Frontend: Spring Boot app (Thymeleaf) in the repo root run with Gradle (`./gradlew bootRun`). See `src/main/resources/templates/chat.html`.
- Backend (AI & APIs): Python FastAPI service located at `fastapi/chatbot/` (main file: `chatbot.py`). The FastAPI app exposes `/api/chat`, `/api/stt`, `/api/tts`, `/api/markets`, `/health`, and a session `reset` endpoint.
- Data: MongoDB holds crawled news used by RAG and `get_latest_news()`; vector store for RAG is managed via OpenAI Vector Store (files `.vector_store_id` and `.vs_state.json` in `fastapi/chatbot/`).

2) Primary workflows & commands
- Start Spring Boot frontend (from repo root):
  - `./gradlew bootRun` (or `./gradlew bootJar` then run jar)
- Start FastAPI backend (from `fastapi/chatbot/`):
  - Ensure env vars (`OPENAI_API_KEY`, `MONGO_URI`, `ECOS_API_KEY`, `FRED_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, `CLOVA_KEY_ID`, `CLOVA_KEY`) are set or placed in `.env`.
  - Create/activate Python venv, install deps (suggested):
    - `python -m venv .venv && source .venv/bin/activate`
    - `pip install fastapi uvicorn openai pymongo apscheduler google-cloud-texttospeech yfinance pandas httpx python-dotenv watchdog`
  - Run: `uvicorn chatbot.chatbot:app --reload --host 0.0.0.0 --port 8002` (run from `fastapi/chatbot/`).
- RAG / Vector store maintenance:
  - `watcher.py` watches `fastapi/chatbot/docs/` and uploads documents to the OpenAI vector store. Run it with `python watcher.py` from `fastapi/chatbot/` (requires `OPENAI_API_KEY`). The watcher creates `.vector_store_id` and `.vs_state.json`.

3) Important project-specific conventions
- Function-calling first: `chatbot.py` exposes a `TOOLS` array describing functions the LLM should call. Agents must prefer invoking these tool functions for real-time data (news, indicators, market):
  - `get_latest_news(count)` — returns latest news from MongoDB (use for any "latest news" questions).
  - `get_indicator(indicator_type)` — must be used for CPI/PPI/GDP/BASE_RATE/TRADE_BALANCE/CURRENT_ACCOUNT/US_FEDFUNDS/US_FED_TARGET questions.
  - `get_market(market_type, ticker?)` — must be used for FX/indices/quotes.
  - `search_docs(query)` — RAG-backed doc search for service/help questions; implemented via client.responses with a file_search tool.
- Tool invocation contract: `run_tool(tool_name, arguments)` returns JSON {ok: bool, markdown: str} (or {ok: False, error}). When simulating behavior or writing tests, follow that return shape.
- Session handling: in-memory session store `SESSIONS` with `MAX_TURNS=20`. Reset endpoint: `POST /api/reset`.
- CORS: `chatbot.py` enables `allow_origins=['*']` for convenience — be aware of security implications when changing this.

4) Key files to inspect when making changes
- `fastapi/chatbot/chatbot.py` — core of the AI backend; pay attention to `SYSTEM_INSTRUCTIONS`, `TOOLS`, `run_tool()`, and the `/api/chat` flow.
- `fastapi/chatbot/watcher.py` — document watcher/uploader that creates/updates vector-store files and `.vector_store_id`.
- `fastapi/chatbot/crawler_rag.py` — crawler used by the scheduler (used in `_job_naver`).
- `fastapi/chatbot/.vector_store_id` and `.vs_state.json` — runtime artifacts; do not delete unless reinitializing RAG.
- `fastapi/chatbot/training_data.jsonl` — contains fine-tune data (if used).
- `build.gradle`, `gradlew` — frontend build and run (Spring Boot).

5) Testing & debugging tips
- Run Gradle tests: `./gradlew test` from repo root. The test report is output under `build/reports/tests/`.
- FastAPI debugging: start `uvicorn` with `--reload` and tail logs. Use `curl` or a REST client to exercise endpoints.
- Example API call (chat):
  - `curl -X POST http://localhost:8002/api/chat -H 'Content-Type: application/json' -d '{"message":"최신 코스피 시황 알려줘", "session_id":"dev"}'`
  - Expect flow: model may call `get_market` → `run_tool` executes and returns `markdown` payload → final response produced.
- To test RAG document ingestion: put a supported file (`.pdf`, `.md`, `.txt`) into `fastapi/chatbot/docs/` and run `python watcher.py`; watch `.vs_state.json` change and `.vector_store_id` to confirm upload.

6) Things to watch for / gotchas
- The code uses an in-repo default `MONGO_URI = "mongodb://localhost:27017"` but README references MongoDB Atlas. Confirm the runtime `MONGO_URI` value in env.
- `FFMPEG` path: the STT flow expects `ffmpeg` on PATH or set `FFMPEG_BIN` env var. If missing, STT will fail.
- GCP TTS requires `GOOGLE_APPLICATION_CREDENTIALS` pointing at a service account key JSON; `tts` endpoint validates path existence.
- `watcher.py` prints the OpenAI key at startup for debugging — treat logs containing API keys as sensitive.
- `chatbot.py` sets `VECTOR_STORE_ID` from `.vector_store_id` or `VECTOR_STORE_ID` env var; ensure consistency between watcher and service deployment.

7) When you change LLM prompts or tools
- Update `SYSTEM_INSTRUCTIONS` and `TOOLS` schema in `chatbot.py` simultaneously. Tests and users depend on exact tool names (`get_latest_news`, `get_indicator`, `get_market`, `search_docs`).

If anything above is unclear or you'd like more examples (unit-test stubs, a `requirements.txt`, or a sample `.env`), tell me which part to expand and I will update this file.
