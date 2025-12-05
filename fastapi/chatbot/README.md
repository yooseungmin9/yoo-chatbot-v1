### yoo-chatbot-v3 아키텍쳐 다이어그램

<img width="681" height="631" alt="Image" src="https://github.com/user-attachments/assets/1f15d608-d244-4cbc-a19c-665d71319616" />

### yoo-chatbot-v0
- GPT-5 + RAG + Open API + MongoDB + Function Calling

### yoo-chatbot-v1 개선 사항

[yoo-chatbot-v1 개선 사항](https://www.notion.so/yoo-chatbot-v1-2b3ca2ee78bc80bba29de68cc55027a2?pvs=21)

- GPT-4o-mini + 코드 구조 개선
- TTS 친화적 답변, 프론트 엔드 개선

### yoo-chatbot-v2 개선 사항

[yoo-chatbot-v2 개선 사항](https://www.notion.so/yoo-chatbot-v2-2bfca2ee78bc805dab7ae2ceb37db0ea?pvs=21)

- 기존 v1 + GPT-4o-mini (파인튜닝 시도) → 모델이 망가짐
- [경제 질답 300개 파인튜닝 파일](./training_data.jsonl)

### yoo-chatbot-v3 개선 사항

[yoo-chatbot-v3 개선사항](https://www.notion.so/yoo-chatbot-v3-2bfca2ee78bc80be8b89ea3c53deff08?pvs=21)

- 기존 v2 + LLaMA 3.1 8B (오픈소스 모델 활용/ GPT-3.5 급 sLLM)
- Langchain과 Tool Calling 활용

### yoo-chatbot-v4 개선 사항

[yoo-chatbot-v4 개선사항](https://www.notion.so/yoo-chatbot-v4-2bfca2ee78bc80bda0cdde2b06b036ae?pvs=21)

- 기존 v3 + Gemma 2 9B (다른 모델 활용/ GPT-4o-mini 급 sLLM) → Tool Calling 지원x
- VectorDB(Qdrant)과 4bit 양자화, LM Studio 시도 → 모델이 기본적으로 양자화 되어 있음.

### APScheduler로 실시간 뉴스 수집
[실시간 네이버 뉴스 수집 크롤러 코드](./crawler_rag.py)

### Watchdog으로 Docs 실시간 감시 및 Vectorstore 업로드
[문서 폴더 감시 → OpenAI 벡터스토어 업로드 코드](./watcher.py)

### Watchdog으로 Docs 실시간 감시 및 로컬 Vectorstore 업로드
[문서 폴더 감시 → 로컬 벡터스토어 업로드 코드](./watcher-local.py)
- 오픈소스 모델로 만들면서 FAISS 활용, 로컬 벡터스토어 구현
- OpenAI 기반이 아닌 로컬 기반의 Watchdog 감시,업로딩 코드
