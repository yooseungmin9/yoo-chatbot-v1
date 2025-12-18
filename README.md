# SUMMARIX - AI 경제 뉴스 분석 챗봇

> 실시간 경제 뉴스, 주가, 환율, 경제지표를 AI로 조회하는 웹서비스

[![Demo](https://img.shields.io/badge/Demo-YouTube-red)](https://youtu.be/Bk_dYeuUDCE?si=dGZZ6Px7Fax4qNhX&t=114)
[![Docs](https://img.shields.io/badge/Docs-Notion-black)](https://www.notion.so/yoo-chatbot-2bfca2ee78bc8089a2e6e8993860d803)
[![Live](https://img.shields.io/badge/Live-yooseungmin.com-blue)](https://www.yooseungmin.com)

---

## Overview

| 항목 | 내용 |
|------|------|
| **프로젝트** | AI 기반 경제 정보 제공 플랫폼 |
| **개발자** | 유승민 |
| **개발 기간** | 2024.09.15 ~ 2024.10.15 (1개월) |
| **배포** | AWS EC2 + Nginx + Let's Encrypt |

---

## Features

| 기능 | 설명 |
|------|------|
| **AI 챗봇** | 자연어로 경제 정보 질문 → 즉시 답변 |
| **실시간 데이터** | 뉴스, 주가, 환율, 금리 조회 |
| **음성 대화** | STT/TTS 지원 (CLOVA + Google Cloud) |
| **RAG 검색** | 경제 용어/서비스 매뉴얼 검색 |

---

## Tech Stack

```
Frontend     Spring Boot + Thymeleaf
Backend      FastAPI + Ollama (Gemma 2 9B)
Database     MongoDB Atlas
APIs         ECOS, FRED, PyKRX, yFinance
Voice        CLOVA STT, Google Cloud TTS
Infra        AWS EC2, Nginx, Docker
```

---

## Architecture

```
User → Nginx (HTTPS) → Spring Boot (8081) → FastAPI (8002)
                                                 ↓
                                          ┌─────────────┐
                                          │ ToolRouter  │ 정규식 패턴 매칭
                                          └─────────────┘
                                                 ↓
                              ┌──────────────────┼──────────────────┐
                              ↓                  ↓                  ↓
                         MongoDB            ECOS/FRED           Gemma 2 9B
                         (뉴스)             (경제지표)           (응답 생성)
```

---

## Quick Start

### 1. 환경 설정

```bash
# Ollama 모델 다운로드
ollama pull gemma2:9b

# Python 환경 설정
cd fastapi/chatbot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 환경변수 (.env)
MONGO_URI=mongodb+srv://...
ECOS_API_KEY=your_key
FRED_API_KEY=your_key
GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
```

### 2. 서버 실행

```bash
# FastAPI
uvicorn chatbot_v4:app --port 8002

# Spring Boot (별도 터미널)
./gradlew bootRun

# 접속: http://localhost:8081/chat
```

### 3. Docker (선택)

```bash
cd fastapi/chatbot
docker-compose up -d
```

---

## Project Structure

```
chatbot-v1/
├── fastapi/chatbot/
│   ├── chatbot_v4.py        # AI 챗봇 서버
│   ├── config.py            # 설정 관리
│   ├── crawler_rag.py       # 뉴스 크롤러
│   ├── tests/               # pytest 테스트
│   └── requirements.txt
├── src/main/
│   ├── java/.../ChatController.java
│   └── resources/templates/chat.html
├── CLAUDE.md                # 상세 문서
└── AWS_DEPLOYMENT_GUIDE.md  # 배포 가이드
```

---

## Performance

| 지표 | 이전 | 현재 | 개선 |
|------|------|------|------|
| 도구 호출 정확도 | 70% | **100%** | +43% |
| 평균 응답 시간 | 3-5초 | **1-2초** | 2-3x |
| 테스트 통과율 | - | **100%** | 16/16 |

---

## Documentation

- [CLAUDE.md](CLAUDE.md) - 전체 프로젝트 가이드 (1,600+ lines)
- [AWS_DEPLOYMENT_GUIDE.md](AWS_DEPLOYMENT_GUIDE.md) - 배포 가이드

---

## Contact

**유승민** · [GitHub](https://github.com/yooseungmin9)

---

*Last Updated: 2025-12-18 · Version 1.5*
