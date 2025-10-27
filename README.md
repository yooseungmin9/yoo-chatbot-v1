# 🧠 AI 기반 경제 뉴스 분석 웹서비스
> 쉽고 빠르게 경제 뉴스의 흐름을 이해할 수 있는 AI 기반 웹서비스 /
> **기획 및 운영, 챗봇 담당: 유승민**  
> https://www.youtube.com/watch?v=Bk_dYeuUDCE / 시연영상 챗봇파트 01:54

---

## 📌 개요
경제 뉴스를 효율적으로 분석하고 핵심 이슈·감성 흐름을 한눈에 보여주는  
**AI 경제 뉴스 분석 플랫폼**입니다.  
AI가 뉴스 요약, 감성 분석, 키워드 추출을 자동화하여  
사용자가 쉽고 빠르게 경제 트렌드를 이해할 수 있도록 돕습니다.

---

## 🎯 프로젝트 목표
1. **AI 기반 경제 뉴스 분석 서비스 완성**
2. 데이터 → 분석 → 시각화 → 서비스화 **전 과정 직접 구현**
3. 대시보드로 뉴스·지표·감성 흐름을 직관적으로 표현
4. **RAG 기반 챗봇으로 실시간 경제질문 대응**

---

## 🧠 담당 역할
| 구분 | 주요 내용 |
|------|------------|
| **기획 총괄, 운영** | 전체 아키텍처 설계, 서비스 플로우 및 발표 구성, 일정관리, 팀 협업 |
| **AI 챗봇 구축** | OpenAI GPT-5 + Function Calling + RAG |
| **데이터 연동** | MongoDB 최신뉴스 / FRED·ECOS 실시간 지표 / yFinance 주가 |
| **STT·TTS 통합** | CLOVA Speech-to-Text, Google Cloud Text-to-Speech |
| **RAG 관리** | 문서 자동 감시(`watcher.py`) 및 Vector Store 갱신 |
| **FastAPI 서버 설계** | `/chat`, `/tts`, `/stt`, `/rag/docs` 등 주요 API 구성 |

---

## 💡 주요 기능

### 🤖 AI 챗봇 (FastAPI 기반)
- GPT-5 기반 Function Calling 구조
- 실시간 경제지표 조회  
  (한국은행 ECOS, 미국 FRED, yFinance, MongoDB 최신기사)
- RAG(Vector Store) 기반 문서 검색 응답
- 음성 입력(STT) 및 음성 출력(TTS) 지원

### 📄 API 구조
```python
# chatbot.py
@app.post("/chat")
    # 1. 요청 수신 및 전처리
    # 2. Function Calling으로 OpenAI 1차 호출
    # 3. MongoDB 최신 기사 또는 RAG 문서 검색
    # 4. OpenAPI 호출을 통해 실시간 정보 응답 (ECOS, FRED, yFinance)
    # 5. OpenAI 2차 호출 (함수 결과 통합)
    # 6. STT/TTS 후처리 및 응답한 JSON을 대화 형태로 송신(응답 반환 및 세션 저장)
```

### 🧾 Function 목록

| 함수명                 | 설명                 |
| ------------------- | ------------------ |
| `get_latest_news()` | DB 최신 뉴스 요약        |
| `get_indicator()`   | ECOS/FRED 실시간 경제지표 |
| `get_market()`      | yFinance 환율/주가     |
| `search_docs()`     | RAG 기반 문서 검색       |

---

## 🧱 전체 시스템 구성도

```
[수집] NAVER News / Youtube API / ECOS / FRED / yFinance
   ↓
[저장] MongoDB Atlas (chatbot_rag)
   ↓
[분석] KoBART Summarizer, Sentiment Dict, GPT Labeling
   ↓
[AI] GPT-5 Function Calling + Vector Store (RAG)
   ↓
[Web] Spring Boot(8081) ↔ FastAPI(8002~8009)
   ↓
[사용자] 대시보드, 뉴스요약, 챗봇, 감성 시각화
```

---

## ⚙️ 기술 스택

| 구분           | 기술                                                |
| ------------ | ------------------------------------------------- |
| Backend (AI) | **FastAPI**, OpenAI GPT-5, transformers, pandas   |
| STT/TTS      | CLOVA Speech-to-Text, Google Cloud Text-to-Speech |
| Database     | MongoDB Atlas                                     |
| Data Source  | ECOS, FRED, yFinance, NAVER News API              |
| Infra        | APScheduler, OpenAI Vector Store (RAG)            |
| Frontend 연동  | Spring Boot + Thymeleaf + Chart.js                |

---

## 🗂️ 폴더 구조 (핵심 파트 요약)

```
│
├── chatbot/
│   ├─ chatbot.py           # GPT-5 Function Calling 챗봇 API
│   ├─ chatbot_rag.py       # RAG 응답 전용 API
│   ├─ watcher.py           # docs 폴더 실시간 감시 → Vector Store 자동 갱신
│   ├─ .vector_store_id     # RAG 스토어 ID
│   └─ .vs_state.json       # 인덱스 상태 메타정보
└── spring/
    └─ ChatController.java  # FastAPI /api/chat 프록시
```

---

## 🔐 환경 변수 예시 (.env)

```env
OPENAI_API_KEY=sk-xxxx
ECOS_API_KEY=xxxx
FRED_API_KEY=xxxx
NAVER_CLIENT_ID=xxxx
NAVER_CLIENT_SECRET=xxxx
MONGO_URI=mongodb+srv://TeamB:team0000@cluster0.mongodb.net/
```

---

## 📆 일정 요약

| 기간          | 주요 작업            |
| ----------- | ---------------- |
| 9/15–9/18   | 기획 및 아키텍처 설계     |
| 9/19–10/09  | 챗봇 구현 + 데이터파이프라인 구축 |
| 10/10–10/14 | 통합 테스트 및 고도화, 시연 준비   |
| 10/15       | 최종 발표 및 시연       |

---

## 📸 결과물

* GPT-5 기반 **AI 경제 챗봇**
* MongoDB 실시간 뉴스 연동 대시보드
* STT/TTS 음성 대화 지원
* 발표용 **시연 웹서비스**

---

## 🪪 License

> 본 프로젝트는 교육용으로 제작되었으며
> 추후 **Apache 2.0 License** 적용을 검토 중입니다.
