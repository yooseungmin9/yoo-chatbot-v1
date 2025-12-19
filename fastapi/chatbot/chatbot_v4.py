# chatbot-v4.py — Gemma 2 9B + 규칙 기반 라우팅 + RAG + Open API + MongoDB

# ===== 아키텍처 =====
# 1. Chatbot 파트: 규칙 기반 Tool Routing + Ollama Gemma 2 9B (응답 생성)
#    - ToolRouter: 정규식 패턴 매칭으로 도구 선택 (100% 안정성)
#    - MongoDB 최신뉴스, ECOS/FRED 경제지표, PyKRX/yfinance 시세, RAG 문서검색
# 2. STT 파트: CLOVA STT + ffmpeg 전처리
# 3. TTS 파트: Google Cloud Text-to-Speech

# ===== 환경변수 로드 =====
from dotenv import load_dotenv
load_dotenv(override=True)

# ===== Pydantic Settings =====
from config import settings

# ===== 기본 임포트 =====
# 표준/서드파티 라이브러리 로드 (FastAPI, Ollama, MongoDB, APScheduler, GCP TTS, yfinance, pandas 등)
import os, logging, subprocess, io, requests, tempfile, re, json
import asyncio
import threading
from typing import Dict, Any, List, Optional, Union
from pydantic import BaseModel, Field
from pathlib import Path
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, UploadFile, File, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from contextlib import asynccontextmanager, suppress
import httpx, html
import aiohttp

from pymongo import MongoClient, DESCENDING
from apscheduler.schedulers.background import BackgroundScheduler
from google.cloud import texttospeech
from crawler_rag import crawl_today
import yfinance as yf
from pykrx import stock
import pandas as pd

# ===== LangChain import (경량화 - Agent 제거) =====
from langchain_ollama import ChatOllama
from langchain_community.vectorstores import FAISS
from langchain_community.document_loaders import DirectoryLoader, UnstructuredWordDocumentLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings

# ===== 구조화된 로깅 =====
import uuid
from contextvars import ContextVar

# 요청별 컨텍스트 변수 (request_id, session_id 추적)
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
session_id_var: ContextVar[str] = ContextVar("session_id", default="-")

class StructuredJsonFormatter(logging.Formatter):
    """JSON 포맷 로그 포매터 (구조화된 로깅)"""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.now(KST).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
            "session_id": session_id_var.get(),
        }

        # 추가 필드 (extra로 전달된 값)
        if hasattr(record, "extra_data"):
            log_data.update(record.extra_data)

        # 예외 정보 추가
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # 소스 위치 (디버그용)
        if record.levelno >= logging.WARNING:
            log_data["location"] = f"{record.filename}:{record.lineno}"

        return json.dumps(log_data, ensure_ascii=False, default=str)

class StructuredLogger:
    """구조화된 로거 래퍼 클래스"""

    def __init__(self, name: str = "chatbot"):
        self._logger = logging.getLogger(name)

    def _log(self, level: int, message: str, **kwargs):
        """추가 데이터와 함께 로그 기록"""
        extra = {"extra_data": kwargs} if kwargs else {}
        self._logger.log(level, message, extra=extra)

    def debug(self, message: str, **kwargs):
        self._log(logging.DEBUG, message, **kwargs)

    def info(self, message: str, **kwargs):
        self._log(logging.INFO, message, **kwargs)

    def warning(self, message: str, **kwargs):
        self._log(logging.WARNING, message, **kwargs)

    def error(self, message: str, **kwargs):
        self._log(logging.ERROR, message, **kwargs)

    def exception(self, message: str, **kwargs):
        """예외 정보와 함께 에러 로그"""
        extra = {"extra_data": kwargs} if kwargs else {}
        self._logger.exception(message, extra=extra)


def setup_logging(json_format: bool = True, level: int = logging.INFO):
    """로깅 설정 초기화

    Args:
        json_format: True면 JSON 포맷, False면 텍스트 포맷
        level: 로그 레벨
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # 기존 핸들러 제거
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # 콘솔 핸들러 추가
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)

    if json_format:
        console_handler.setFormatter(StructuredJsonFormatter())
    else:
        # 개발용 텍스트 포맷
        text_format = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
        console_handler.setFormatter(logging.Formatter(text_format))

    root_logger.addHandler(console_handler)

    # 외부 라이브러리 로그 레벨 조정
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("pymongo").setLevel(logging.WARNING)

# 환경변수로 JSON 로깅 여부 결정 (개발: False, 운영: True)
_USE_JSON_LOGGING = os.environ.get("LOG_FORMAT", "json").lower() == "json"
setup_logging(json_format=_USE_JSON_LOGGING, level=logging.INFO)

# 구조화된 로거 인스턴스
log = StructuredLogger("chatbot")

# ===== 기준시각 포맷 함수 =====
# KST 타임존 상수
KST = ZoneInfo("Asia/Seoul")

# ===== 주요 티커 상수 (중복 리터럴 방지) =====
TICKER_KOSPI = "^KS11"
TICKER_KOSDAQ = "^KQ11"
TICKER_DOW = "^DJI"
TICKER_SP500 = "^GSPC"
TICKER_NASDAQ = "^IXIC"
TICKER_USD_KRW = "USDKRW=X"
TICKER_JPY_KRW = "JPYKRW=X"
TICKER_EUR_USD = "EURUSD=X"

# ===== 매직 넘버 상수 정의 =====
# 동시성 제어
MAX_API_CONCURRENT = 10          # 외부 API 최대 동시 호출 수
MAX_YF_CONCURRENT = 2            # yfinance 최대 동시 호출 수 (rate limit 회피)
MAX_KRX_CONCURRENT = 3           # PyKRX 최대 동시 호출 수 (rate limit 엄격)
MAX_OLLAMA_CONCURRENT = 3        # Ollama LLM 최대 동시 호출 수 (GPU 메모리 보호)
MAX_THREAD_WORKERS = 10          # ThreadPoolExecutor 워커 수

# 타임아웃 (초)
LLM_TIMEOUT_SECONDS = 60         # LLM 응답 타임아웃
HTTP_TIMEOUT_SECONDS = 30        # HTTP 요청 타임아웃
FRED_TIMEOUT_SECONDS = 20        # FRED API 타임아웃
ECOS_TIMEOUT_SECONDS = 30        # ECOS API 타임아웃
STT_TIMEOUT_SECONDS = 60         # STT API 타임아웃

# 캐시
QUOTE_CACHE_TTL_SECONDS = 60     # 시세 캐시 TTL (초)
CACHE_REFRESH_INTERVAL = 180     # 백그라운드 캐시 갱신 간격 (초) - Yahoo rate limit 방지
CACHE_BATCH_SIZE = 2             # 한 번에 조회할 티커 수 (rate limit 방지)
CACHE_BATCH_DELAY = 3.0          # 배치 간 딜레이 (초)
YF_REQUEST_DELAY = 1.0           # yfinance 개별 요청 간 딜레이 (초)
LRU_CACHE_SIZE = 1000            # LRU 캐시 최대 크기

# 세션
MAX_SESSION_TURNS = 20           # 세션당 최대 대화 턴 수
MAX_HISTORY_TURNS = 10           # LLM 컨텍스트에 포함할 최대 히스토리 턴

# 뉴스
DEFAULT_NEWS_COUNT = 5           # 기본 뉴스 조회 개수
MAX_NEWS_COUNT = 50              # 최대 뉴스 조회 개수
MIN_NEWS_COUNT = 1               # 최소 뉴스 조회 개수
NEWS_ROUTER_MAX_COUNT = 20       # 라우터에서 제한하는 최대 뉴스 개수

# LLM 설정
LLM_NUM_CTX = 8192               # Gemma 2 컨텍스트 크기
LLM_NUM_PREDICT = 512            # 최대 생성 토큰 수
LLM_TEMPERATURE = 0.5           # LLM 온도 (창의성)
OLLAMA_BASE_URL = "http://localhost:11434"

# RAG 설정
RAG_CHUNK_SIZE = 500             # 문서 청크 크기
RAG_CHUNK_OVERLAP = 50           # 청크 오버랩
RAG_TOP_K = 3                    # 검색 결과 개수
RAG_CONTEXT_MAX_CHARS = 200      # 컨텍스트 문서 최대 길이

# MongoDB
MONGO_MAX_POOL_SIZE = 50         # 최대 연결 풀 크기
MONGO_MIN_POOL_SIZE = 10         # 최소 연결 풀 크기
MONGO_TIMEOUT_MS = 3000          # 서버 선택 타임아웃 (밀리초)

# 데이터 조회 기간 (일)
FRED_LOOKBACK_DAYS = 90          # FRED 데이터 조회 기간
ECOS_LOOKBACK_DAYS = 365         # ECOS 데이터 조회 기간
TRADE_LOOKBACK_DAYS = 730        # 무역 데이터 조회 기간 (2년)

# 숫자 검증
SUSPICIOUS_NUMBER_THRESHOLD = 1000  # 의심 숫자 임계값
NUMBER_TOLERANCE = 0.01          # 숫자 비교 허용 오차

# 스케줄러
CRAWLER_INTERVAL_MINUTES = 10    # 뉴스 크롤러 실행 간격 (분)
CRAWLER_LIMIT_PER_RUN = 10       # 크롤러 1회 실행당 수집 개수
MISFIRE_GRACE_TIME = 60          # 스케줄러 미스파이어 허용 시간 (초)

# 환율 배율
JPY_MULTIPLY = 100               # 엔화 표시 배율 (100엔 기준)

# ===== 동시성 제어 =====
# 세마포어: 외부 API 동시 호출 제한
API_SEMAPHORE = asyncio.Semaphore(MAX_API_CONCURRENT)
YF_SEMAPHORE = asyncio.Semaphore(MAX_YF_CONCURRENT)
KRX_SEMAPHORE = asyncio.Semaphore(MAX_KRX_CONCURRENT)
OLLAMA_SEMAPHORE = asyncio.Semaphore(MAX_OLLAMA_CONCURRENT)

# ThreadPoolExecutor: 동기 라이브러리(yfinance, pykrx) 비동기 래핑용
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_THREAD_WORKERS)

# ===== 시세 캐시 (TTL 기반) =====
# 구조: {ticker: {"data": {...}, "expires_at": datetime}}
QUOTE_CACHE: Dict[str, Dict[str, Any]] = {}
QUOTE_CACHE_TTL = timedelta(seconds=QUOTE_CACHE_TTL_SECONDS)
QUOTE_CACHE_LOCK = asyncio.Lock()

# ===== 세션 매니저 (Thread-Safe) =====
class SessionManager:
    """Thread-Safe 세션 관리자

    - Lock을 사용하여 동시 접근 시 Race Condition 방지
    - 세션별 최대 턴 수 제한 (메모리 관리)
    """

    def __init__(self, max_turns: int = MAX_SESSION_TURNS):
        self._sessions: Dict[str, List[Dict[str, str]]] = {}
        self._lock = threading.Lock()
        self._max_turns = max_turns

    def get(self, session_id: str) -> List[Dict[str, str]]:
        """세션 조회 (없으면 생성)"""
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = []
            # 복사본 반환 (외부 수정 방지)
            return list(self._sessions[session_id])

    def add_turn(self, session_id: str, role: str, content: str) -> None:
        """대화 턴 추가"""
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = []
            self._sessions[session_id].append({"role": role, "content": content})
            # 최대 턴 수 제한
            if len(self._sessions[session_id]) > 2 * self._max_turns:
                self._sessions[session_id] = self._sessions[session_id][-2 * self._max_turns:]

    def clear(self, session_id: str = None) -> None:
        """세션 초기화 (session_id 없으면 전체 초기화)"""
        with self._lock:
            if session_id:
                self._sessions.pop(session_id, None)
            else:
                self._sessions.clear()

    def count(self) -> int:
        """현재 세션 수"""
        with self._lock:
            return len(self._sessions)

# 싱글톤 인스턴스
session_manager = SessionManager(max_turns=MAX_SESSION_TURNS)

# 하위 호환성을 위한 래퍼 함수
def get_session(session_id: str) -> List[Dict[str, str]]:
    """세션 조회 (하위 호환성)"""
    return session_manager.get(session_id)

def add_turn(session_id: str, role: str, content: str) -> None:
    """대화 턴 추가 (하위 호환성)"""
    session_manager.add_turn(session_id, role, content)

def format_kst_human(ts_iso: str) -> str:
    """ISO8601 KST 문자열을 '2025년 11월 29일 02시' 형식으로 변환"""
    try:
        dt = datetime.fromisoformat(ts_iso)  # tz 포함 ISO 파싱[web:79]
        return dt.strftime("%Y년 %m월 %d일 %H시")  # 2025년 11월 29일 02시[web:80]
    except Exception:
        return ts_iso  # 실패하면 원문 그대로

# ===== Pydantic 모델 정의 =====

# === Request 모델 ===
class ChatRequest(BaseModel):
    """채팅 요청 모델"""
    message: str = Field(..., min_length=1, description="사용자 메시지")
    session_id: str = Field(default="default", description="세션 ID")

class TTSRequest(BaseModel):
    """TTS 요청 모델"""
    text: str = Field(..., min_length=1, description="음성 변환할 텍스트")
    lang: str = Field(default="ko-KR", description="언어 코드")
    voice: Optional[str] = Field(default=None, description="음성 종류")

class ResetRequest(BaseModel):
    """세션 리셋 요청 모델"""
    session_id: Optional[str] = Field(default=None, description="리셋할 세션 ID (없으면 전체)")

# === Response 모델 ===
class ToolResult(BaseModel):
    """도구 실행 결과 (일관된 반환 타입)"""
    output: Optional[str] = Field(default=None, description="성공 시 출력")
    error: Optional[str] = Field(default=None, description="에러 메시지")
    data: Optional[Dict[str, Any]] = Field(default=None, description="원본 데이터")

    @property
    def is_success(self) -> bool:
        return self.error is None and self.output is not None

    def to_dict(self) -> Dict[str, Any]:
        """dict 변환 (하위 호환성)"""
        if self.error:
            return {"error": self.error}
        return {"output": self.output} if self.output else {"error": "데이터 없음"}

class ChatResponse(BaseModel):
    """채팅 응답 모델"""
    answer: str = Field(..., description="챗봇 응답")
    session_id: str = Field(default="default", description="세션 ID")
    error: Optional[str] = Field(default=None, description="에러 메시지")

class HealthResponse(BaseModel):
    """헬스체크 응답 모델"""
    status: str = Field(default="ok", description="서버 상태")
    ts_kst: str = Field(..., description="서버 시각 (KST)")

class QuoteData(BaseModel):
    """시세 데이터 모델"""
    price: Optional[float] = Field(default=None, description="현재가")
    change: Optional[float] = Field(default=None, description="변동액")
    changePct: Optional[float] = Field(default=None, description="변동률 (%)")
    volume: Optional[int] = Field(default=None, description="거래량")
    ticker: Optional[str] = Field(default=None, description="티커 코드")
    ts_kst: Optional[str] = Field(default=None, description="조회 시각")

    def get_change_safe(self) -> float:
        """변동액 안전 조회 (None이면 0)"""
        return self.change if self.change is not None else 0.0

    def get_change_pct_safe(self) -> float:
        """변동률 안전 조회 (None이면 0)"""
        return self.changePct if self.changePct is not None else 0.0

# === 헬퍼 함수 ===
def make_tool_result(output: str = None, error: str = None, data: dict = None) -> ToolResult:
    """ToolResult 생성 헬퍼"""
    return ToolResult(output=output, error=error, data=data)

def make_success(output: str, data: dict = None) -> Dict[str, Any]:
    """성공 결과 dict 생성 (하위 호환성)"""
    return {"output": output, "data": data} if data else {"output": output}

def make_error(error: str) -> Dict[str, Any]:
    """에러 결과 dict 생성 (하위 호환성)"""
    return {"error": error}

def _get_safe_float(data: Dict[str, Any], key: str, default: float = 0.0) -> float:
    """dict에서 안전하게 float 값 조회 (None 처리)"""
    value = data.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _get_safe_int(data: Dict[str, Any], key: str, default: int = 0) -> int:
    """dict에서 안전하게 int 값 조회 (None 처리)"""
    value = data.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

# =============================================================
# CHATBOT (RAG + 뉴스 + 지표 + 시세 + Langchain + 세션/라우트)
# =============================================================

# ===== 시스템 프롬프트 =====
SYSTEM_INSTRUCTIONS = """
# 역할
경제 뉴스 분석 AI 챗봇. 한국어로 물어보면 한국어만, 영어로 물어보면 영어만 사용.

# 서비스 소개 (일반 대화 시 참고)
저는 SUMMARIX 경제 뉴스 분석 챗봇입니다. 다음 기능을 제공합니다:
- 최신 경제 뉴스 조회 및 요약
- 한국/미국 주식 실시간 시세 조회 (삼성전자, 애플 등)
- 코스피, 코스닥 지수 조회
- 달러/원, 엔/원 환율 조회
- 한국은행 기준금리, GDP 등 경제지표 조회
- 미국 연방기금금리 조회

# 일반 대화 처리
- 경제, 투자, 금융 관련 일반 질문에 친절하게 답변합니다.
- 경제 용어 설명, 투자 기초 개념 등을 설명할 수 있습니다.
- 단, 투자 조언이나 특정 종목 추천은 하지 않습니다.
- 모르는 질문에는 솔직히 "정확한 정보를 확인하기 어렵습니다"라고 답변합니다.

# 숫자 정확성 원칙 (실시간 데이터 관련)
1. 모든 가격, 지수, 환율, 퍼센트 수치는 **반드시 [DATA] 태그 안의 값만 사용**
2. [DATA] 태그가 없으면 구체적인 숫자를 언급하지 않음
3. 숫자를 반올림, 변환, 추정하지 않음 - 있는 그대로만 전달
4. "약", "대략", "정도", "추정" 같은 불확실한 표현 금지

# 응답 생성 규칙
## [DATA] 태그가 있을 때
- 태그 안의 숫자를 **한 글자도 바꾸지 말고** 그대로 응답에 포함
- 예: [DATA]price=52000[/DATA] → "52,000원" (천 단위 쉼표만 허용)

## [DATA] 태그가 없을 때 (일반 대화)
- 일반 경제/금융 질문 → 친절하게 설명
- 실시간 수치가 필요한 질문 → "현재 조회할 수 없습니다. '삼성전자 주가' 처럼 구체적으로 질문해 주세요."

# 응답 형식
- 국내 주식: "{종목명}({코드})의 현재 주가는 {price}원입니다. 전일 대비 {change}원({changePct}%) 변동했습니다."
- 해외 주식: "{종목명}({티커})의 현재 주가는 ${price}입니다."
- 환율: "현재 {통화} 환율은 {price}원입니다."
- 마무리: "더 궁금한 부분이 있으신가요?"

# 금지 사항
- [DATA] 태그 밖에서 가격/지수/환율 숫자 생성 금지
- "72,500원", "1,350원" 등 임의의 숫자 사용 금지
- "약 5만원대", "50,000원 정도" 같은 추정 표현 금지
- "도구호출:", "도구결과:", "[DATA]" 텍스트를 응답에 노출 금지
"""

# ===== 도구별 프롬프트 템플릿 (할루시네이션 방지 강화) =====
TOOL_PROMPT_TEMPLATE = """사용자 질문: {user_message}

[DATA]
{tool_output}
[/DATA]

위 [DATA] 태그 안의 정보만 사용하여 답변하세요.

중요 규칙:
1. [DATA] 안의 숫자를 **절대 변경하지 마세요** (반올림, 단위 변환 금지)
2. [DATA]에 없는 정보는 언급하지 마세요
3. 응답에 [DATA] 태그를 포함하지 마세요
4. 100~150자로 간결하게 작성하세요
5. 마지막에 "더 궁금한 부분이 있으신가요?" 추가"""

# ===== 도구 함수 래퍼 정의 =====
def get_latest_news_wrapper(count: int) -> dict:
    """최신 뉴스 조회 래퍼"""
    try:
        n = max(MIN_NEWS_COUNT, min(NEWS_ROUTER_MAX_COUNT, count))
        rows = fetch_latest_topn_from_mongo(n)
        return {"output": format_topn_md(rows)}
    except Exception as e:
        return {"error": f"뉴스 조회 실패: {str(e)}"}

def get_indicator_wrapper(indicator_type: str) -> dict:
    """경제지표 조회 래퍼"""
    t = indicator_type.upper().strip()

    try:
        if t == "CPI":
            data = get_cpi_data()
        elif t == "PPI":
            data = get_ppi_data()
        elif t == "GDP":
            data = get_gdp_data()
        elif t == "BASE_RATE":
            data = get_base_rate()
        elif t == "TRADE_BALANCE":
            data = get_trade_balance()
        elif t == "CURRENT_ACCOUNT":
            data = get_current_account()

        elif t == "US_FEDFUNDS":
            # 목표범위(DFEDTARU/L)를 우선 사용 (일간 데이터로 최신)
            d = get_us_fed_funds_latest(True)
            if "error" not in d:
                rng = f"{d['lower']:.2f}–{d['upper']:.2f}{d.get('unit','%')}"
                data = f"미국 연방기금금리 목표범위\n• 현재: {rng} (기준: {d['date']})"
            else:
                # fallback: 실효 연방기금금리 (월간, 지연됨)
                d = get_us_fed_funds_latest(False)
                if "error" in d:
                    return {"error": "미국 연방기금금리 조회 실패. FRED API에서 데이터를 가져올 수 없습니다."}
                data = f"미국 실효 연방기금금리(FEDFUNDS)\n• 최신값: {d['value']:.2f}{d.get('unit','%')} (기준: {d['date']})\n※ 월간 데이터로 실제 현재 금리와 다를 수 있습니다."

        elif t == "US_FED_TARGET":
            d = get_us_fed_funds_latest(True)
            if "error" in d:
                return {"error": "미국 연방기금금리 목표범위 조회 실패. FRED API에서 데이터를 가져올 수 없습니다."}
            rng = f"{d['lower']:.2f}–{d['upper']:.2f}{d.get('unit','%')}"
            data = f"미국 연방기금금리 목표범위\n• 범위: {rng} (기준: {d['date']})"
        
        else:
            return {"error": f"지원하지 않는 지표입니다: {t}"}
        
        # 통일된 반환 형식
        return {"output": data}
    
    except Exception as e:
        log.error("get_indicator 실패", indicator_type=t, error=str(e))
        return {"error": f"{t} 조회 실패: {str(e)}"}

# ===== 통합 티커 매핑 (종목명 → 티커 변환용) =====
# 사용자 입력(한글/영문)을 yfinance/pykrx 티커로 변환
STOCK_TICKER_MAP: Dict[str, str] = {
    # ===== 한국 대형주 =====
    "삼성전자": "005930.KS",
    "네이버": "035420.KS",
    "NAVER": "035420.KS",
    "SK하이닉스": "000660.KS",
    "삼성바이오로직스": "207940.KS",
    "삼성바이오": "207940.KS",
    "LG에너지솔루션": "373220.KS",
    "LG에너지": "373220.KS",
    "LG": "003550.KS",
    "현대차": "005380.KS",
    "현대자동차": "005380.KS",
    "기아": "000270.KS",
    "기아차": "000270.KS",
    "카카오": "035720.KS",
    "포스코": "005490.KS",
    "포스코홀딩스": "005490.KS",
    "셀트리온": "068270.KS",
    "LG전자": "066570.KS",
    "현대모비스": "012330.KS",
    "삼성SDI": "006400.KS",
    "삼성에스디아이": "006400.KS",
    "KB금융": "105560.KS",
    "신한지주": "055550.KS",
    "하나금융지주": "086790.KS",
    "삼성물산": "028260.KS",
    "LG화학": "051910.KS",
    "한국전력": "015760.KS",
    "한전": "015760.KS",
    "SK텔레콤": "017670.KS",
    "SKT": "017670.KS",
    "KT": "030200.KS",

    # ===== 미국 빅테크 =====
    "애플": "AAPL",
    "apple": "AAPL",
    "마이크로소프트": "MSFT",
    "microsoft": "MSFT",
    "구글": "GOOGL",
    "google": "GOOGL",
    "알파벳": "GOOGL",
    "아마존": "AMZN",
    "amazon": "AMZN",
    "메타": "META",
    "meta": "META",
    "페이스북": "META",
    "facebook": "META",
    "엔비디아": "NVDA",
    "nvidia": "NVDA",
    "테슬라": "TSLA",
    "tesla": "TSLA",

    # ===== 미국 기타 =====
    "오라클": "ORCL",
    "oracle": "ORCL",
    "넷플릭스": "NFLX",
    "netflix": "NFLX",
    "디즈니": "DIS",
    "disney": "DIS",
    "인텔": "INTC",
    "intel": "INTC",
    "AMD": "AMD",
    "amd": "AMD",
    "코카콜라": "KO",
    "맥도날드": "MCD",
    "나이키": "NKE",
    "스타벅스": "SBUX",
    "월마트": "WMT",
    "코스트코": "COST",
    "비자": "V",
    "마스터카드": "MA",
    "JP모건": "JPM",
    "뱅크오브아메리카": "BAC",
    "버크셔": "BRK-B",
    "버크셔해서웨이": "BRK-B",
    "존슨앤존슨": "JNJ",
    "화이자": "PFE",
    "보잉": "BA",
    "엑슨모빌": "XOM",
    "쉐브론": "CVX",
}

# 티커 자동 변환 유틸 (통합 STOCK_TICKER_MAP 사용)
def resolve_ticker(ticker: str) -> str:
    """종목명/티커를 yfinance/pykrx 호환 형식으로 변환"""
    ticker_clean = ticker.strip()

    # 이미 티커 형식인 경우
    if ticker_clean.endswith((".KS", ".KQ")):
        return ticker_clean

    # 통합 매핑에서 확인
    for name, tkr in STOCK_TICKER_MAP.items():
        if name.lower() in ticker_clean.lower():
            log.info("종목 자동 변환", input=ticker, output=tkr)
            return tkr

    # 6자리 숫자 → 한국 주식으로 추정
    if re.match(r'^\d{6}$', ticker_clean):
        return f"{ticker_clean}.KS"

    # 영문 대문자 1~5자 → 해외 티커로 추정
    if re.match(r'^[A-Z]{1,5}$', ticker_clean.upper()):
        return ticker_clean.upper()

    return ticker_clean

# ===== 규칙 기반 도구 라우터 =====
class ToolRouter:
    """규칙 기반 도구 선택 및 파라미터 추출"""

    def __init__(self):
        # (패턴, 도구명, 파라미터 추출 함수) 튜플 리스트
        self.rules = [
            # 뉴스 관련
            (r'(최신|최근|오늘|어제).{0,5}뉴스', 'get_latest_news', self._extract_news_params),
            (r'뉴스.{0,5}(\d+)개', 'get_latest_news', self._extract_news_params),

            # 지수 관련 (주가보다 우선 매칭)
            (r'코스피|KOSPI', 'get_market', lambda q: {'market_type': 'KOSPI', 'ticker': ''}),
            (r'코스닥|KOSDAQ', 'get_market', lambda q: {'market_type': 'KOSDAQ', 'ticker': ''}),

            # 환율 관련 (패턴 확장)
            (r'달러.{0,5}환율|환율.{0,5}달러|원달러|달러\s*가격|원화', 'get_market', lambda q: {'market_type': 'USD_KRW', 'ticker': ''}),
            (r'엔.{0,5}환율|환율.{0,5}엔|엔화', 'get_market', lambda q: {'market_type': 'JPY_KRW', 'ticker': ''}),
            (r'유로.{0,5}달러|EURUSD|유로\s*환율', 'get_market', lambda q: {'market_type': 'EUR_USD', 'ticker': ''}),

            # 경제지표 관련
            (r'(한국|국내).{0,5}(기준금리|금리)', 'get_indicator', lambda q: {'indicator_type': 'BASE_RATE'}),
            (r'gdp|지디피|경제성장', 'get_indicator', lambda q: {'indicator_type': 'GDP'}),
            (r'(한국|국내).{0,5}(cpi|소비자물가)', 'get_indicator', lambda q: {'indicator_type': 'CPI'}),
            (r'(미국|연준).{0,5}(기준금리|금리|FEDFUNDS)', 'get_indicator', lambda q: {'indicator_type': 'US_FEDFUNDS'}),
            (r'무역수지', 'get_indicator', lambda q: {'indicator_type': 'TRADE_BALANCE'}),
            (r'경상수지', 'get_indicator', lambda q: {'indicator_type': 'CURRENT_ACCOUNT'}),

            # 주가 관련 (범용 패턴 - 모든 주가 질문 캡처)
            (r'주가|주식.{0,3}(가격|얼마)|stock\s*price|시세', 'get_market', self._extract_stock_params_flexible),
            # "XX 얼마야?" 패턴 (종목명 + 얼마)
            (r'.{1,10}(얼마|가격|시세)', 'get_market', self._extract_stock_params_flexible),

            # 서비스 도움말 (패턴 대폭 확장)
            (r'(사용법|도움말|메뉴얼|가이드|사용방법|기능|뭐해|뭘\s*할\s*수|무엇을|어떤\s*것|어떻게\s*사용|이\s*서비스|이\s*웹|챗봇|소개|설명해)', 'search_docs', self._extract_docs_params),
        ]

    def _extract_news_params(self, query: str) -> dict:
        """뉴스 개수 추출"""
        match = re.search(r'(\d+)개', query)
        count = int(match.group(1)) if match else DEFAULT_NEWS_COUNT
        count = max(MIN_NEWS_COUNT, min(NEWS_ROUTER_MAX_COUNT, count))
        return {'count': count}

    def _extract_stock_params_flexible(self, query: str) -> dict:
        """주식 종목명 추출 (통합 STOCK_TICKER_MAP 사용, 복수 종목 지원)"""
        query_lower = query.lower()
        found_tickers = []

        # 1. 통합 매핑에서 모든 매칭 종목 확인
        for name, ticker in STOCK_TICKER_MAP.items():
            if name.lower() in query_lower:
                if ticker not in found_tickers:
                    log.info("패턴 매칭: 종목명", name=name, ticker=ticker)
                    found_tickers.append(ticker)

        # 2. 6자리 숫자 티커 (한국 주식)
        for match in re.finditer(r'(\d{6})', query):
            ticker = f"{match.group(1)}.KS"
            if ticker not in found_tickers:
                log.info("패턴 매칭: 숫자 티커", ticker=ticker)
                found_tickers.append(ticker)

        # 3. 영문 대문자 1~5자 티커 (해외 주식)
        excluded = {'THE', 'AND', 'FOR', 'ARE', 'BUT', 'NOT', 'YOU', 'ALL', 'CAN', 'HAD', 'HER', 'WAS', 'ONE', 'OUR', 'OUT'}
        for match in re.finditer(r'\b([A-Z]{1,5})\b', query.upper()):
            ticker = match.group(1)
            if ticker not in excluded and ticker not in found_tickers:
                log.info("패턴 매칭: 영문 티커", ticker=ticker)
                found_tickers.append(ticker)

        # 4. 결과 반환
        if len(found_tickers) > 1:
            # 복수 종목: MULTI_QUOTE 타입으로 반환
            log.info("복수 종목 감지", tickers=found_tickers)
            return {'market_type': 'MULTI_QUOTE', 'tickers': found_tickers}
        elif len(found_tickers) == 1:
            return {'market_type': 'QUOTE', 'ticker': found_tickers[0]}
        else:
            # 종목 특정 불가 → 빈 티커 반환 (LLM이 사용자에게 확인 요청)
            log.warning("종목 특정 불가", query=query)
            return {'market_type': 'QUOTE', 'ticker': ''}

    def _extract_docs_params(self, query: str) -> dict:
        """문서 검색 쿼리 추출"""
        return {'query': query}

    def route(self, query: str) -> Optional[Dict[str, Any]]:
        """쿼리를 분석하여 매칭되는 도구와 파라미터 반환 (단일 매칭)"""
        query_lower = query.lower()

        for pattern, tool_name, param_extractor in self.rules:
            if re.search(pattern, query_lower):
                try:
                    params = param_extractor(query)
                    return {
                        'tool': tool_name,
                        'params': params
                    }
                except Exception as e:
                    log.error("파라미터 추출 실패", pattern=pattern, error=str(e))
                    continue

        return None  # 매칭 안 됨 → 일반 대화

    def route_multiple(self, query: str) -> List[Dict[str, Any]]:
        """쿼리를 분석하여 매칭되는 모든 도구와 파라미터 반환 (복합 질문 지원)"""
        query_lower = query.lower()
        matched = []
        matched_tools = set()  # 중복 도구 방지

        for pattern, tool_name, param_extractor in self.rules:
            if re.search(pattern, query_lower):
                try:
                    params = param_extractor(query)
                    # 같은 도구라도 파라미터가 다르면 추가 (예: 여러 종목)
                    tool_key = f"{tool_name}:{str(sorted(params.items()))}"
                    if tool_key not in matched_tools:
                        matched.append({
                            'tool': tool_name,
                            'params': params
                        })
                        matched_tools.add(tool_key)
                except Exception as e:
                    log.error("파라미터 추출 실패", pattern=pattern, error=str(e))
                    continue

        return matched

# 라우터 인스턴스 생성
router = ToolRouter()

# ===== PyKRX 시세 조회 =====

def _extract_krx_code(ticker_code: str) -> str | None:
    """티커 코드에서 KRX 6자리 코드 추출"""
    if re.match(r'^\d{6}$', ticker_code):
        return ticker_code
    if ticker_code.endswith(('.KS', '.KQ')):
        return ticker_code.replace('.KS', '').replace('.KQ', '')
    return None

def _format_quote_raw(q: dict, ticker_display: str) -> dict:
    """조회 결과를 LangChain용 숫자 형식으로 포맷팅"""
    price = q.get('price')
    if price is None:
        return {"error": f"{ticker_display} 가격 데이터 없음"}

    change = q.get('change')
    change_pct = q.get('changePct')
    date_str = q.get('ts_kst') or q.get('date') or datetime.now(KST).strftime("%Y-%m-%d")

    # ISO 형식이면 날짜만 추출
    if isinstance(date_str, str) and 'T' in date_str:
        date_str = date_str.split('T')[0]

    change_str = f"{change:.0f}" if change is not None else "N/A"
    change_pct_str = f"{change_pct:.2f}" if change_pct is not None else "N/A"

    return {"output": f"price={price}, change={change_str}, changePct={change_pct_str}, date={date_str}"}

def _try_fetch_krx(krx_code: str, ticker_display: str) -> dict | None:
    """PyKRX에서 시세 조회 시도"""
    q = fetch_quote_krx(krx_code)
    if q and q.get('price') is not None:
        return _format_quote_raw(q, ticker_display)
    return None

def _try_fetch_yf(yf_ticker: str, ticker_display: str) -> dict | None:
    """yfinance에서 시세 조회 시도"""
    q = fetch_quote_yf(yf_ticker)
    if q and q.get('price') is not None:
        return _format_quote_raw(q, ticker_display)
    return None


def fetch_quote_formatted(ticker: str) -> dict:
    """PyKRX 우선 → yfinance fallback (LangChain용 숫자 형식)"""
    ticker_code = resolve_ticker(ticker.strip())
    log.info("시세 조회 요청", input_ticker=ticker, resolved_ticker=ticker_code)

    # 1. 한국 주식: PyKRX 우선 시도
    krx_code = _extract_krx_code(ticker_code)
    if krx_code:
        result = _try_fetch_krx(krx_code, ticker)
        if result:
            return result

    # 2. 글로벌 주식/지수: yfinance
    yf_ticker = f"{ticker_code}.KS" if re.match(r'^\d{6}$', ticker_code) else ticker_code
    result = _try_fetch_yf(yf_ticker, ticker)
    if result:
        return result

    return {"error": f"{ticker} 데이터 없음"}


# ===== 시장 데이터 포맷팅 함수 =====

def _format_index_output(name: str, data: Dict[str, Any]) -> Dict[str, str]:
    """지수 데이터 포맷팅 (타입 안전)"""
    price = data.get("price")
    if price is None:
        return make_error(f"{name} 지수 데이터를 가져올 수 없습니다.")
    ch = _get_safe_float(data, "change")
    pct = _get_safe_float(data, "changePct")
    sign = "+" if ch >= 0 else ""
    return make_success(f"**{name} 지수 (실시간)**\n• 현재가: {price:,.2f}\n• 변동: {sign}{ch:.2f} ({sign}{pct:.2f}%)")


def _format_fx_output(name: str, data: Dict[str, Any], multiply: int = 1) -> Dict[str, str]:
    """환율 데이터 포맷팅 (타입 안전)"""
    price = data.get("price")
    if price is None:
        return make_error(f"{name} 환율 데이터를 가져올 수 없습니다.")
    display_price = price * multiply if multiply > 1 else price
    ch = _get_safe_float(data, "change") * multiply
    pct = _get_safe_float(data, "changePct")
    sign = "+" if ch >= 0 else ""
    unit = "원" if "원" in name else "달러"
    return make_success(f"**{name} 환율 (실시간)**\n• 현재: {display_price:,.2f}{unit}\n• 변동: {sign}{ch:.2f} ({sign}{pct:.2f}%)")


def _format_quote_output(ticker: str, data: Dict[str, Any]) -> Dict[str, str]:
    """개별 종목 시세 포맷팅 (타입 안전)"""
    if data.get("error"):
        return make_error(str(data["error"]))
    price = data.get("price")
    if price is None:
        return make_error(f"{ticker} 시세를 가져올 수 없습니다.")
    ch = _get_safe_float(data, "change")
    pct = _get_safe_float(data, "changePct")
    sign = "+" if ch >= 0 else ""
    # 원화/달러 구분
    ticker_str = data.get("ticker") or ""
    is_korean = ticker_str.endswith((".KS", ".KQ"))
    if is_korean:
        return make_success(f"**{ticker} 시세 (실시간)**\n• 현재가: {price:,.0f}원\n• 변동: {sign}{ch:,.0f}원 ({sign}{pct:.2f}%)")
    else:
        return make_success(f"**{ticker} 시세 (실시간)**\n• 현재가: ${price:,.2f}\n• 변동: {sign}${ch:.2f} ({sign}{pct:.2f}%)")


def _format_market_summary(results: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """시장 요약 포맷팅 (타입 안전)"""
    lines = ["**📊 시장 요약 (실시간)**\n"]

    # 지수
    lines.append("**[지수]**")
    for ticker, name in [(TICKER_KOSPI, "코스피"), (TICKER_KOSDAQ, "코스닥"), (TICKER_DOW, "다우"), (TICKER_SP500, "S&P500")]:
        data = results.get(ticker, {})
        price = data.get("price")
        if price is not None:
            pct = _get_safe_float(data, "changePct")
            sign = "+" if pct >= 0 else ""
            lines.append(f"• {name}: {price:,.2f} ({sign}{pct:.2f}%)")

    # 환율
    lines.append("\n**[환율]**")
    for ticker, name in [(TICKER_USD_KRW, "달러/원"), (TICKER_JPY_KRW, "엔/원(100)")]:
        data = results.get(ticker, {})
        price = data.get("price")
        if price is not None:
            display = price * JPY_MULTIPLY if ticker == TICKER_JPY_KRW else price
            pct = _get_safe_float(data, "changePct")
            sign = "+" if pct >= 0 else ""
            lines.append(f"• {name}: {display:,.2f} ({sign}{pct:.2f}%)")

    return make_success("\n".join(lines))

# ===== 시장 데이터 조회 (비동기) =====

# 시장 타입별 설정 (ticker, formatter, kwargs)
_MARKET_TYPE_CONFIG = {
    "KOSPI": (TICKER_KOSPI, _format_index_output, {"name": "코스피"}),
    "KOSDAQ": (TICKER_KOSDAQ, _format_index_output, {"name": "코스닥"}),
    "USD_KRW": (TICKER_USD_KRW, _format_fx_output, {"name": "달러/원"}),
    "JPY_KRW": (TICKER_JPY_KRW, _format_fx_output, {"name": "엔/원", "multiply": JPY_MULTIPLY}),
    "EUR_USD": (TICKER_EUR_USD, _format_fx_output, {"name": "유로/달러"}),
}

async def _handle_market_summary() -> dict:
    """시장 요약 조회 (병렬)"""
    tickers = [TICKER_KOSPI, TICKER_KOSDAQ, TICKER_DOW, TICKER_SP500, TICKER_USD_KRW, TICKER_JPY_KRW]
    results = await fetch_quotes_parallel(tickers)
    return _format_market_summary(results)

async def _handle_quote(ticker: str) -> dict:
    """개별 종목 시세 조회"""
    if not ticker or ticker.strip() == "":
        return {"output": "종목을 특정할 수 없습니다. 종목명이나 티커 코드를 알려주시겠어요? (예: 삼성전자, AAPL, 005930)"}
    resolved = resolve_ticker(ticker)
    data = await fetch_quote_cached_async(resolved)
    return _format_quote_output(ticker, data)

async def _handle_multi_quote(tickers: List[str]) -> dict:
    """복수 종목 시세 조회 (병렬)"""
    if not tickers:
        return {"output": "종목을 특정할 수 없습니다. 종목명이나 티커 코드를 알려주시겠어요?"}

    # 티커 해석
    resolved_tickers = [resolve_ticker(t) for t in tickers]

    # 병렬 조회
    results = await fetch_quotes_parallel(resolved_tickers)

    # 결과 포맷팅
    output_lines = ["**📊 복수 종목 시세 조회 결과**\n"]
    for original, resolved in zip(tickers, resolved_tickers):
        data = results.get(resolved, {})
        if data.get("error"):
            output_lines.append(f"• **{original}**: 조회 실패 - {data['error']}")
        elif data.get("price") is not None:
            price = data["price"]
            ch = _get_safe_float(data, "change")
            pct = _get_safe_float(data, "changePct")
            sign = "+" if ch >= 0 else ""
            # 원화/달러 구분
            is_korean = resolved.endswith((".KS", ".KQ"))
            if is_korean:
                output_lines.append(f"• **{original}**: {price:,.0f}원 ({sign}{pct:.2f}%)")
            else:
                output_lines.append(f"• **{original}**: ${price:,.2f} ({sign}{pct:.2f}%)")
        else:
            output_lines.append(f"• **{original}**: 데이터 없음")

    return {"output": "\n".join(output_lines)}

async def get_market_wrapper_async(market_type: str, ticker: str = "", tickers: List[str] = None) -> dict:
    """시장 데이터 조회 래퍼 (비동기 - 캐시 활용)"""
    try:
        market_type = market_type.strip().upper()

        # 특수 케이스: MARKET_SUMMARY, QUOTE, MULTI_QUOTE
        if market_type == "MARKET_SUMMARY":
            return await _handle_market_summary()
        if market_type == "MULTI_QUOTE":
            return await _handle_multi_quote(tickers or [])
        if market_type == "QUOTE":
            return await _handle_quote(ticker)

        # 일반 케이스: 설정 기반 디스패치
        config = _MARKET_TYPE_CONFIG.get(market_type)
        if not config:
            return {"error": f"지원하지 않는 시장 타입: {market_type}"}

        ticker_symbol, formatter, kwargs = config
        data = await fetch_quote_cached_async(ticker_symbol)
        return formatter(data=data, **kwargs)

    except Exception as e:
        return {"error": f"시장 데이터 조회 실패: {str(e)}"}

# ===== 벡터스토어 초기화 (앱 시작 시 1회) =====
embeddings = HuggingFaceEmbeddings(
    model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # 한국어 지원
)

# 문서 로드 및 벡터스토어 생성 (최초 1회 또는 문서 업데이트 시)
def create_vectorstore():
    """문서를 벡터스토어로 변환"""
    
    # 문서 로드
    loader = DirectoryLoader(
        path="./docs",
        glob="**/*.docx",
        loader_cls=UnstructuredWordDocumentLoader
    )
    documents = loader.load()
    
    # 청크 분할
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=RAG_CHUNK_SIZE,
        chunk_overlap=RAG_CHUNK_OVERLAP
    )
    chunks = text_splitter.split_documents(documents)
    
    # 벡터스토어 생성 및 저장 (watcher-local.py와 동일 경로)
    vectorstore = FAISS.from_documents(chunks, embeddings)
    vectorstore.save_local("./vector_store")
    return vectorstore

# 벡터스토어 경로 (watcher-local.py와 동일하게 통일)
VECTOR_STORE_PATH = "./vector_store"

# 벡터스토어 로드 (앱 시작 시)
try:
    vectorstore = FAISS.load_local(VECTOR_STORE_PATH, embeddings, allow_dangerous_deserialization=True)
    log.info("벡터스토어 로드 완료", path=VECTOR_STORE_PATH)
except Exception as e:
    log.warning("벡터스토어 로드 실패, 새로 생성", error=str(e))
    vectorstore = create_vectorstore()

# ===== 검색 함수 =====
def search_docs_wrapper(query: str) -> dict:
    """벡터스토어 문서 검색 래퍼"""
    docs = vectorstore.similarity_search(query, k=RAG_TOP_K)
    if not docs:
        return {"output": "관련 문서를 찾지 못했습니다."}
    
    # LLM 호출 없이 문서 내용만 반환
    context = "\n\n".join([f"• {doc.page_content[:RAG_CONTEXT_MAX_CHARS]}" for doc in docs])
    return {"output": f"검색 결과:\n{context}"}

# ===== Ollama LLM (규칙 기반 라우팅용) =====
llm = ChatOllama(
    model="gemma2:9b",
    base_url=OLLAMA_BASE_URL,
    temperature=LLM_TEMPERATURE,
    num_ctx=LLM_NUM_CTX,
    num_predict=LLM_NUM_PREDICT,
)

# 스트리밍용 LLM (별도 인스턴스)
llm_stream = ChatOllama(
    model="gemma2:9b",
    base_url=OLLAMA_BASE_URL,
    temperature=LLM_TEMPERATURE,
    num_ctx=LLM_NUM_CTX,
    num_predict=LLM_NUM_PREDICT,
)

# ===== 규칙 기반 채팅 함수 (Gemma 2 9B 최적화) =====
GREETING_KEYWORDS = ["안녕", "hello", "hi", "반가", "처음", "감사", "반갑", "초보"]
GREETING_RESPONSE = "안녕하세요! 저는 경제 뉴스와 실시간 경제 지표, 주가 정보를 제공하며, 경제 용어 설명으로 경제 학습을 도와드립니다. 무엇이 궁금하신가요?"

# 도구 함수 매핑 (동기 함수용 - ThreadPool 래핑에 사용)
TOOL_MAP_SYNC = {
    'get_latest_news': get_latest_news_wrapper,
    'get_indicator': get_indicator_wrapper,
    'search_docs': search_docs_wrapper
}

# 지원하는 도구 목록
SUPPORTED_TOOLS = {'get_latest_news', 'get_indicator', 'get_market', 'search_docs'}

async def _execute_tool_async(tool_name: str, params: dict) -> dict:
    """도구 실행 공통 함수 (비동기 전용)

    - get_market: 비동기 함수 직접 호출 (캐시 + 병렬 처리 지원)
    - 나머지 도구: ThreadPoolExecutor로 비동기 래핑 (MongoDB, ECOS/FRED API, FAISS 검색)
    """
    if tool_name not in SUPPORTED_TOOLS:
        return {"error": f"알 수 없는 도구: {tool_name}"}

    loop = asyncio.get_running_loop()

    if tool_name == 'get_market':
        # 비동기 시세 조회 (캐시 + 세마포어 적용)
        # MULTI_QUOTE 지원
        market_type = params.get('market_type', '')
        if market_type == 'MULTI_QUOTE':
            return await get_market_wrapper_async(
                market_type='MULTI_QUOTE',
                tickers=params.get('tickers', [])
            )
        return await get_market_wrapper_async(
            market_type=market_type,
            ticker=params.get('ticker', '')
        )
    elif tool_name == 'get_latest_news':
        # MongoDB 조회 - ThreadPoolExecutor로 비동기 래핑
        async with API_SEMAPHORE:
            return await loop.run_in_executor(
                EXECUTOR,
                lambda: get_latest_news_wrapper(count=params.get('count', 5))
            )
    elif tool_name == 'get_indicator':
        # ECOS/FRED API 조회 - ThreadPoolExecutor로 비동기 래핑
        async with API_SEMAPHORE:
            return await loop.run_in_executor(
                EXECUTOR,
                lambda: get_indicator_wrapper(indicator_type=params.get('indicator_type', ''))
            )
    elif tool_name == 'search_docs':
        # FAISS 벡터 검색 - ThreadPoolExecutor로 비동기 래핑
        async with API_SEMAPHORE:
            return await loop.run_in_executor(
                EXECUTOR,
                lambda: search_docs_wrapper(query=params.get('query', ''))
            )
    return {"error": "도구 실행 실패"}

# ===== 도구 결과 검증 및 정제 =====
def _extract_numbers_from_text(text: str) -> set:
    """텍스트에서 모든 숫자 추출 (검증용)"""
    # 정수, 소수, 천 단위 쉼표 포함 숫자 추출
    patterns = [
        r'-?\d{1,3}(?:,\d{3})*(?:\.\d+)?',  # 1,234,567.89
        r'-?\d+(?:\.\d+)?',  # 1234.56
    ]
    numbers = set()
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for m in matches:
            # 쉼표 제거 후 정규화
            normalized = m.replace(',', '')
            try:
                num = float(normalized)
                numbers.add(num)
                # 원본 형태도 저장 (쉼표 포함)
                numbers.add(m)
            except ValueError:
                pass
    return numbers

def _validate_tool_result(tool_result: dict) -> dict:
    """도구 결과 검증 및 정제"""
    if "error" in tool_result:
        return tool_result

    output = tool_result.get("output", "")
    if not output:
        return {"error": "도구 결과가 비어있습니다"}

    # 결과에서 핵심 숫자 추출하여 메타데이터로 저장
    extracted_numbers = _extract_numbers_from_text(str(output))

    return {
        "output": output,
        "_valid_numbers": extracted_numbers,  # 검증용 메타데이터
        "_raw_output": output  # 원본 보존
    }

def _build_tool_prompt(user_message: str, tool_output: str) -> str:
    """도구 결과를 자연어로 변환하기 위한 프롬프트 생성 (강화된 버전)"""
    return TOOL_PROMPT_TEMPLATE.format(
        user_message=user_message,
        tool_output=tool_output
    )

# ===== 응답 후처리 필터 (할루시네이션 감지) =====
# 의심스러운 추정 표현 패턴
HALLUCINATION_PATTERNS = [
    r'약\s*\d',           # "약 50000"
    r'대략\s*\d',         # "대략 1000"
    r'정도\s*(?:입니다|예요|이에요)',  # "5만원 정도입니다"
    r'추정\s*(?:됩니다|입니다)',       # "추정됩니다"
    r'예상\s*(?:됩니다|입니다)',       # "예상됩니다"
    r'아마\s*\d',         # "아마 50000"
    r'대충\s*\d',         # "대충 5만"
    r'\d+\s*(?:쯤|가량|내외)',  # "5만원쯤", "50000가량"
]

# 컴파일된 패턴 (성능 최적화)
HALLUCINATION_REGEX = re.compile('|'.join(HALLUCINATION_PATTERNS), re.IGNORECASE)

def _detect_hallucination_patterns(response: str) -> List[str]:
    """응답에서 할루시네이션 의심 패턴 감지"""
    return HALLUCINATION_REGEX.findall(response)

def _remove_data_tags(text: str) -> str:
    """[DATA] 태그 제거"""
    text = re.sub(r'\[/?DATA\]', '', text)
    text = re.sub(r'\[DATA\].*?\[/DATA\]', '', text, flags=re.DOTALL)
    return text

def _replace_hallucination_patterns(text: str) -> str:
    """할루시네이션 패턴을 안전한 표현으로 대체"""
    matches = _detect_hallucination_patterns(text)
    if not matches:
        return text

    log.warning("할루시네이션 의심 패턴 감지", matches=matches)
    for pattern in HALLUCINATION_PATTERNS:
        text = re.sub(pattern, '[정확한 수치는 다시 조회해 주세요]', text, flags=re.IGNORECASE)
    return text

def _is_number_valid(num: float, valid_numbers: set) -> bool:
    """숫자가 유효 목록에 있는지 확인 (부동소수점 오차 허용)"""
    for valid in valid_numbers:
        if isinstance(valid, (int, float)):
            if abs(num - valid) < NUMBER_TOLERANCE:
                return True
        elif isinstance(valid, str):
            try:
                valid_num = float(valid.replace(',', ''))
                if abs(num - valid_num) < NUMBER_TOLERANCE:
                    return True
            except ValueError:
                continue
    return False

def _find_suspicious_numbers(text: str, valid_numbers: set) -> List[float]:
    """도구 결과에 없는 의심스러운 숫자 찾기 (임계값 이상만)"""
    response_numbers = _extract_numbers_from_text(text)
    suspicious = []

    for num in response_numbers:
        if isinstance(num, (int, float)) and num >= SUSPICIOUS_NUMBER_THRESHOLD:
            if not _is_number_valid(num, valid_numbers):
                suspicious.append(num)

    return suspicious

def _filter_response(response: str, valid_numbers: set = None) -> str:
    """응답 후처리 필터링 (할루시네이션 감지 및 정제)"""
    # 1. [DATA] 태그 제거
    filtered = _remove_data_tags(response)

    # 2. 할루시네이션 패턴 대체
    filtered = _replace_hallucination_patterns(filtered)

    # 3. 도구 결과에 없는 숫자 감지 (로깅만)
    if valid_numbers:
        suspicious = _find_suspicious_numbers(filtered, valid_numbers)
        if suspicious:
            log.warning("도구 결과에 없는 숫자 감지", suspicious_numbers=suspicious)

    # 4. 불필요한 공백 정리
    filtered = re.sub(r'\n{3,}', '\n\n', filtered)
    return filtered.strip()

def _build_chat_prompt(history: list, user_message: str) -> str:
    """일반 대화용 프롬프트 생성"""
    messages = [{"role": "system", "content": SYSTEM_INSTRUCTIONS}]
    for turn in history[-MAX_HISTORY_TURNS:]:
        messages.append({"role": turn['role'], "content": turn['content']})
    messages.append({"role": "user", "content": user_message})
    return "\n\n".join([f"{msg['role']}: {msg['content']}" for msg in messages])

async def _invoke_llm_async(prompt: str) -> str:
    """LLM 호출 (세마포어 + 타임아웃 적용)

    - OLLAMA_SEMAPHORE: 동시 요청 3개 제한 (GPU 메모리 보호)
    - LLM_TIMEOUT_SECONDS: 60초 타임아웃 (무한 대기 방지)
    """
    async with OLLAMA_SEMAPHORE:
        loop = asyncio.get_running_loop()
        try:
            response = await asyncio.wait_for(
                loop.run_in_executor(EXECUTOR, llm.invoke, prompt),
                timeout=LLM_TIMEOUT_SECONDS
            )
            return response.content if hasattr(response, "content") else str(response)
        except asyncio.TimeoutError:
            log.warning("LLM 타임아웃", timeout_seconds=LLM_TIMEOUT_SECONDS)
            raise TimeoutError(f"응답 생성 시간이 {LLM_TIMEOUT_SECONDS}초를 초과했습니다.")

async def chat_with_agent_async(user_message: str, session_id: str = "default") -> str:
    """규칙 기반 라우팅 + Gemma 2 9B 응답 생성 (비동기 버전)

    - 도구 실행: _execute_tool_async 사용 (캐시 + 세마포어 적용)
    - LLM 호출: _invoke_llm_async 사용 (세마포어 + 타임아웃 적용)
    - 할루시네이션 방지: 도구 결과 검증 + 응답 후처리 필터
    """

    # 1. 인사 감지 시 즉시 반환
    if any(kw in user_message.lower() for kw in GREETING_KEYWORDS):
        add_turn(session_id, "user", user_message)
        add_turn(session_id, "assistant", GREETING_RESPONSE)
        return GREETING_RESPONSE

    valid_numbers = set()  # 도구 결과의 유효 숫자 (검증용)

    try:
        # 2. 규칙 기반 라우팅으로 도구 선택 (복합 질문 지원)
        route_results = router.route_multiple(user_message)

        if route_results:
            # 여러 도구 실행 및 결과 합치기
            all_tool_outputs = []

            for route_result in route_results:
                tool_name = route_result['tool']
                params = route_result['params']
                log.info("도구 호출", tool=tool_name, params=params)

                tool_result = await _execute_tool_async(tool_name, params)

                # 에러는 로깅만 하고 계속 진행 (일부 실패해도 다른 결과 반환)
                if "error" in tool_result:
                    log.warning("도구 실행 실패", tool=tool_name, error=tool_result['error'])
                    all_tool_outputs.append(f"[{tool_name}] 조회 실패: {tool_result['error']}")
                    continue

                # 3. 도구 결과 검증 및 정제
                validated_result = _validate_tool_result(tool_result)
                if "error" in validated_result:
                    log.warning("도구 결과 검증 실패", tool=tool_name, error=validated_result['error'])
                    all_tool_outputs.append(f"[{tool_name}] 검증 실패: {validated_result['error']}")
                    continue

                tool_output = validated_result.get("output", "")
                if tool_output:
                    all_tool_outputs.append(f"[{tool_name}] {tool_output}")

                # 유효 숫자 합치기
                if "_valid_numbers" in validated_result:
                    valid_numbers.update(validated_result["_valid_numbers"])

            # 모든 도구가 실패한 경우
            if not all_tool_outputs:
                error_msg = "죄송합니다. 요청하신 정보를 조회하지 못했습니다."
                add_turn(session_id, "user", user_message)
                add_turn(session_id, "assistant", error_msg)
                return error_msg

            # 4. 도구 결과를 Gemma 2로 자연어 변환 (세마포어 + 타임아웃 적용)
            combined_output = "\n".join(all_tool_outputs)
            context_prompt = _build_tool_prompt(user_message, combined_output)
            raw_answer = await _invoke_llm_async(context_prompt)
        else:
            # 5. 일반 대화 (도구 없이 Gemma 2만 사용, 세마포어 + 타임아웃 적용)
            history = get_session(session_id)
            prompt = _build_chat_prompt(history, user_message)
            raw_answer = await _invoke_llm_async(prompt)

        # 7. 응답 후처리 필터 (할루시네이션 감지 및 정제)
        final_answer = _filter_response(raw_answer, valid_numbers)

        # 8. 세션 저장
        add_turn(session_id, "user", user_message)
        add_turn(session_id, "assistant", final_answer)
        return final_answer

    except Exception as e:
        log.exception("채팅 처리 실패", user_message=user_message[:100])
        return f"죄송합니다. 오류가 발생했습니다: {str(e)}"

# ===== RAG 벡터스토어 ID =====
# ENV 우선, 없으면 .vector_store_id 파일에서 로드
VS_ID_PATH = Path(".vector_store_id")
VS_ID_FILE = VS_ID_PATH.read_text().strip() if VS_ID_PATH.exists() else ""
VS_ID = settings.vector_store_id or VS_ID_FILE
if not VS_ID:
    log.warning("VectorStore ID가 비어있습니다")
else:
    log.info("VectorStore ID 로드됨", vs_id=VS_ID[:20] + "..." if len(VS_ID) > 20 else VS_ID)

# ===== MongoDB 클라이언트 매니저 =====
class MongoClientManager:
    """MongoDB 연결 관리자 (싱글톤 패턴)

    - Lazy initialization으로 필요 시에만 연결 생성
    - Connection pooling 설정 포함
    """

    def __init__(self):
        self._client: Optional[MongoClient] = None
        self._uri = settings.mongo_uri
        self._db_name = settings.mongo_db_name
        self._coll_name = settings.mongo_coll_name

    @property
    def client(self) -> MongoClient:
        """MongoDB 클라이언트 반환 (Lazy initialization)"""
        if self._client is None:
            self._client = MongoClient(
                self._uri,
                maxPoolSize=MONGO_MAX_POOL_SIZE,
                minPoolSize=MONGO_MIN_POOL_SIZE,
                serverSelectionTimeoutMS=MONGO_TIMEOUT_MS
            )
        return self._client

    @property
    def db(self):
        """데이터베이스 반환"""
        return self.client[self._db_name]

    @property
    def collection(self):
        """컬렉션 반환"""
        return self.db[self._coll_name]

    def ensure_indexes(self) -> None:
        """인덱스 생성 확인"""
        coll = self.collection
        coll.create_index([("published_at", DESCENDING)])
        coll.create_index([("collected_at", DESCENDING)])
        log.info("MongoDB 인덱스 확인 완료", collection=self._coll_name)

    def close(self) -> None:
        """연결 종료"""
        if self._client is not None:
            self._client.close()
            self._client = None
            log.info("MongoDB 연결 종료", db=self._db_name)

# 싱글톤 인스턴스
mongo_manager = MongoClientManager()

# 하위 호환성을 위한 래퍼 함수/상수
MONGO_URI = settings.mongo_uri
DB_NAME = settings.mongo_db_name
COLL_NAME = settings.mongo_coll_name

def _get_mongo_client():
    """MongoDB 클라이언트 반환 (하위 호환성)"""
    return mongo_manager.client

def _get_db():
    """데이터베이스 반환 (하위 호환성)"""
    return mongo_manager.db

def _ensure_indexes():
    """인덱스 생성 (하위 호환성)"""
    mongo_manager.ensure_indexes()

# ===== MongoDB 조회 유틸 =====
# 최신 N건 뉴스 집계/날짜 KST 포맷팅
def fetch_latest_topn_from_mongo(n: int = 5):
    coll = _get_db()[COLL_NAME]
    pipeline = [
        {"$addFields": {"_p": {"$ifNull": ["$published_at", "$collected_at"]}}},
        {"$sort": {"_p": -1}},
        {"$limit": int(n)},
        {"$project": {"_id": 0, "title": 1, "url": 1, "published_at": 1}},
    ]
    rows = list(coll.aggregate(pipeline))
    for r in rows:
        pa = r.get("published_at")
        if isinstance(pa, datetime):
            if pa.tzinfo is None: pa = pa.replace(tzinfo=timezone.utc)
            r["published_at"] = pa.astimezone(KST).strftime("%Y-%m-%d")
        elif isinstance(pa, str):
            pass
        else:
            r["published_at"] = ""
    return rows

def format_topn_md(rows):
    """뉴스 목록을 TTS 친화적인 자연스러운 문장으로 변환"""
    if not rows:
        return "현재 최신 경제 뉴스가 없습니다."

    # 오늘 날짜 (상단에서 import한 datetime, KST 사용)
    today = datetime.now(KST)
    date_readable = f"{today.month}월 {today.day}일"
    
    out = [f"{date_readable} 최신 경제 뉴스를 알려드리겠습니다.\n"]
    
    for i, r in enumerate(rows, 1):
        title = (r.get("title") or "").strip() or "제목 없음"
        out.append(f"{i}번째 뉴스는 {title}입니다.\n")
    
    return "\n".join(out)

# ===== FRED =====
# API 키/엔드포인트 상수 (Pydantic Settings에서 로드)
FRED_KEY = settings.fred_api_key
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# ===== FRED 조회 유틸 =====
# 관측치 조회(빈값 필터), FEDFUNDS/목표범위 처리
async def _fred_observations_async(series_id: str) -> list:
    params = {
        "series_id": series_id,
        "api_key": FRED_KEY,
        "file_type": "json",
        "observation_start": (datetime.now() - timedelta(days=FRED_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    }
    async with httpx.AsyncClient(timeout=FRED_TIMEOUT_SECONDS) as client:
        r = await client.get(FRED_BASE, params=params)
        r.raise_for_status()
        obs = r.json().get("observations", []) or []
        return [o for o in obs if o.get("value") not in ("", ".")]

def get_us_fed_funds_latest(use_target_range: bool = False) -> dict:
    """FEDFUNDS(월) 또는 DFEDTARU/L(일) 최신값 반환"""
    try:
        if use_target_range:
            # 목표 범위 상한/하한 동시 조회
            up_params = {
                "series_id": "DFEDTARU",
                "api_key": FRED_KEY,
                "file_type": "json",
                "observation_start": (datetime.now() - timedelta(days=FRED_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
            }
            lo_params = {
                "series_id": "DFEDTARL",
                "api_key": FRED_KEY,
                "file_type": "json",
                "observation_start": (datetime.now() - timedelta(days=FRED_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
            }

            up_r = requests.get(FRED_BASE, params=up_params, timeout=FRED_TIMEOUT_SECONDS)
            lo_r = requests.get(FRED_BASE, params=lo_params, timeout=FRED_TIMEOUT_SECONDS)
            
            up_r.raise_for_status()
            lo_r.raise_for_status()
            
            # 빈 값 필터링
            up = [o for o in up_r.json().get("observations", []) if o.get("value") not in ("", ".")]
            lo = [o for o in lo_r.json().get("observations", []) if o.get("value") not in ("", ".")]
            
            if not up or not lo:
                raise RuntimeError("target range observations empty")
            
            up_last, lo_last = up[-1], lo[-1]
            date = up_last["date"]
            upper = float(up_last["value"])
            lower = float(lo_last["value"])
            return {
                "date": date,
                "value": upper,
                "lower": lower,
                "upper": upper,
                "unit": "%",
                "source": "FRED"
            }
        else:
            # FEDFUNDS 단일 조회
            params = {
                "series_id": "FEDFUNDS",
                "api_key": FRED_KEY,
                "file_type": "json",
                "observation_start": (datetime.now() - timedelta(days=ECOS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
            }
            r = requests.get(FRED_BASE, params=params, timeout=FRED_TIMEOUT_SECONDS)
            r.raise_for_status()
            
            obs = [o for o in r.json().get("observations", []) if o.get("value") not in ("", ".")]
            
            if not obs:
                raise RuntimeError("fedfunds observations empty")
            
            last = obs[-1]
            return {
                "date": last["date"],
                "value": float(last["value"]),
                "unit": "%",
                "source": "FRED"
            }
    except requests.Timeout:
        return {"error": "FRED 응답 지연(Timeout)", "source": "FRED"}
    except Exception as e:
        return {"error": f"FRED 조회 실패: {e}", "source": "FRED"}

# ===== ECOS =====
# BOK ECOS 엔드포인트/키 상수 (Pydantic Settings에서 로드)
ECOS_API_KEY = settings.ecos_api_key
ECOS_BASE = "https://ecos.bok.or.kr/api"

# ===== 공통 에러 메시지 상수 =====
ERR_NO_DATA = "데이터 없음"
ERR_API_TIMEOUT = "API 응답 지연"

# ===== HTTP 클라이언트 매니저 =====
class HttpClientManager:
    """비동기 HTTP 클라이언트 관리자 (싱글톤 패턴)

    - Lazy initialization으로 필요 시에만 클라이언트 생성
    - 앱 종료 시 명시적 close 호출 필요
    """

    def __init__(self, timeout: int = 30):
        self._client: Optional[httpx.AsyncClient] = None
        self._timeout = timeout

    @property
    def client(self) -> httpx.AsyncClient:
        """httpx 비동기 클라이언트 반환 (Lazy initialization)"""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        """클라이언트 종료"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            log.info("httpx 클라이언트 종료", timeout=self._timeout)

# 싱글톤 인스턴스
http_manager = HttpClientManager(timeout=HTTP_TIMEOUT_SECONDS)

# 하위 호환성을 위한 래퍼 함수
def _get_httpx_client() -> httpx.AsyncClient:
    """httpx 비동기 클라이언트 반환 (하위 호환성)"""
    return http_manager.client

async def fetch_ecos_stat_by_code_async(stat_code: str, start_ym: str = None, end_ym: str = None) -> dict:
    """ECOS API 비동기 조회"""
    try:
        if not end_ym:
            end_ym = datetime.now(KST).strftime("%Y%m")
        if not start_ym:
            start_dt = datetime.now(KST) - timedelta(days=365)
            start_ym = start_dt.strftime("%Y%m")
        url = f"{ECOS_BASE}/StatisticSearch/{ECOS_API_KEY}/json/kr/1/100/{stat_code}/M/{start_ym}/{end_ym}/"

        client = _get_httpx_client()
        r = await client.get(url)
        if r.status_code != 200:
            return {"error": f"API {r.status_code}"}
        rows = (r.json().get("StatisticSearch") or {}).get("row", [])
        if not rows:
            return {"error": ERR_NO_DATA}
        return {"ok": True, "data": rows}
    except httpx.TimeoutException:
        log.error("ECOS 타임아웃", stat_code=stat_code)
        return {"error": f"ECOS {ERR_API_TIMEOUT}"}
    except Exception as e:
        log.exception("ECOS 코드 조회 오류", stat_code=stat_code)
        return {"error": str(e)}

def fetch_ecos_stat_by_code(stat_code: str, start_ym: str = None, end_ym: str = None) -> dict:
    """ECOS API 동기 조회 (ThreadPool에서 호출됨)"""
    try:
        if not end_ym:
            end_ym = datetime.now(KST).strftime("%Y%m")
        if not start_ym:
            start_dt = datetime.now(KST) - timedelta(days=365)
            start_ym = start_dt.strftime("%Y%m")
        url = f"{ECOS_BASE}/StatisticSearch/{ECOS_API_KEY}/json/kr/1/100/{stat_code}/M/{start_ym}/{end_ym}/"
        r = requests.get(url, timeout=ECOS_TIMEOUT_SECONDS)
        if r.status_code != 200:
            return {"error": f"API {r.status_code}"}
        rows = (r.json().get("StatisticSearch") or {}).get("row", [])
        if not rows:
            return {"error": ERR_NO_DATA}
        return {"ok": True, "data": rows}
    except requests.Timeout:
        log.error("ECOS 타임아웃", stat_code=stat_code)
        return {"error": f"ECOS {ERR_API_TIMEOUT}"}
    except Exception as e:
        log.exception("ECOS 코드 조회 오류", stat_code=stat_code)
        return {"error": str(e)}

def fetch_all_key_statistics() -> dict:
    """100대 지표 목록 조회"""
    try:
        url = f"{ECOS_BASE}/KeyStatisticList/{ECOS_API_KEY}/json/kr/1/200/"
        r = requests.get(url, timeout=ECOS_TIMEOUT_SECONDS)
        if r.status_code != 200:
            return {"error": f"API {r.status_code}"}
        rows = (r.json().get("KeyStatisticList") or {}).get("row", [])
        if not rows:
            return {"error": ERR_NO_DATA}
        return {"ok": True, "indicators": rows}
    except Exception as e:
        log.exception("ECOS 100대 지표 조회 오류", error=str(e))
        return {"error": str(e)}

# CPI/PPI/GDP/무역/경상/기준금리 포맷
def get_cpi_data() -> str:
    res = fetch_ecos_stat_by_code("901Y009")
    if "error" in res: return f"CPI 조회 실패: {res['error']}"
    d = res["data"]; latest = d[-1]; prev = d[-2] if len(d) >= 2 else None
    value, time = latest.get("DATA_VALUE","N/A"), latest.get("TIME","")
    out = [ "**소비자물가지수(CPI)**", f"• 최신값: {value} (기준: {time})" ]
    if prev:
        try:
            change = float(value) - float(prev.get("DATA_VALUE", 0))
            out.append(f"• 전월 대비: {change:+.2f}%p")
        except Exception: pass
    return "\n".join(out)

def get_ppi_data() -> str:
    res = fetch_ecos_stat_by_code("404Y014")
    if "error" in res: return f"PPI 조회 실패: {res['error']}"
    latest = res["data"][-1]
    return f"**생산자물가지수(PPI)**\n• 최신값: {latest.get('DATA_VALUE','N/A')} (기준: {latest.get('TIME','')})"

def get_gdp_data() -> str:
    res = fetch_ecos_stat_by_code(
        "200Y101",
        start_ym=(datetime.now(KST) - timedelta(days=730)).strftime("%Y%m"),
        end_ym=datetime.now(KST).strftime("%Y%m")
    )
    if "error" in res: return f"GDP 조회 실패: {res['error']}"
    latest = res["data"][-1]
    return f"**GDP 성장률**\n• 최신값: {latest.get('DATA_VALUE','N/A')}% (기준: {latest.get('TIME','')})"

def get_trade_balance() -> str:
    exp = fetch_ecos_stat_by_code("901Y011"); imp = fetch_ecos_stat_by_code("901Y012")
    if "error" in exp or "error" in imp: return "무역수지 조회 실패"
    try:
        e = float(exp["data"][-1]["DATA_VALUE"]); i = float(imp["data"][-1]["DATA_VALUE"])
        bal = e - i; t = exp["data"][-1]["TIME"]
        return f"**무역수지**\n• 수출: ${e:,.0f}백만\n• 수입: ${i:,.0f}백만\n• 무역수지: ${bal:+,.0f}백만 (기준: {t})"
    except Exception:
        return "무역수지 데이터 파싱 오류"

def get_current_account() -> str:
    res = fetch_ecos_stat_by_code("301Y013")
    if "error" in res: return f"경상수지 조회 실패: {res['error']}"
    latest = res["data"][-1]
    return f"**경상수지**\n• 최신값: ${latest.get('DATA_VALUE','N/A')}백만 (기준: {latest.get('TIME','')})"

def get_base_rate() -> str:
    """한국은행 기준금리 조회 (722Y001 = 한국은행 기준금리)"""
    # 722Y001: 한국은행 기준금리 (정확한 통계표 코드)
    res = fetch_ecos_stat_by_code("722Y001")
    if "error" in res:
        # 백업: 901Y001 시도
        res = fetch_ecos_stat_by_code("901Y001")
        if "error" in res:
            return f"기준금리 조회 실패: {res['error']}. 한국은행 ECOS API에서 데이터를 가져올 수 없습니다."
    if not res.get("data"):
        return "기준금리 데이터가 없습니다."
    latest = res["data"][-1]
    value = latest.get('DATA_VALUE', 'N/A')
    time_str = latest.get('TIME', '')
    return f"**한국은행 기준금리**\n• 현재 금리: {value}% (기준: {time_str})"

# ===== yfinance 유틸 =====
# 주요 지수/원자재/금리 티커 매핑 (개별 종목은 STOCK_TICKER_MAP 사용)
INDEX_MAP: Dict[str, Dict[str, str]] = {
    # 한국 지수
    "KOSPI":  {"ticker": TICKER_KOSPI, "name": "코스피"},
    "KOSDAQ": {"ticker": TICKER_KOSDAQ, "name": "코스닥"},

    # 미국 지수
    "DOW":     {"ticker": TICKER_DOW,    "name": "다우존스 산업평균"},
    "SP500":   {"ticker": TICKER_SP500,  "name": "S&P 500"},
    "NASDAQ":  {"ticker": TICKER_NASDAQ, "name": "나스닥 종합"},
    "RUSSELL": {"ticker": "^RUT",        "name": "러셀 2000"},
    "VIX":     {"ticker": "^VIX",        "name": "VIX 변동성 지수"},

    # 유럽 지수
    "EURO_STOXX50": {"ticker": "^STOXX50E", "name": "Euro Stoxx 50"},
    "FTSE100":      {"ticker": "^FTSE",     "name": "FTSE 100"},
    "DAX":          {"ticker": "^GDAXI",    "name": "독일 DAX"},

    # 아시아 지수
    "NIKKEI225": {"ticker": "^N225",     "name": "니케이 225"},
    "TOPIX":     {"ticker": "^TOPX",     "name": "TOPIX"},
    "SHANGHAI":  {"ticker": "000001.SS", "name": "상하이 종합"},
    "HANG_SENG": {"ticker": "^HSI",      "name": "항셍 지수"},

    # 원자재
    "WTI_OIL":   {"ticker": "CL=F", "name": "WTI 원유 선물"},
    "BRENT_OIL": {"ticker": "BZ=F", "name": "브렌트유 선물"},
    "GOLD":      {"ticker": "GC=F", "name": "금 선물"},
    "SILVER":    {"ticker": "SI=F", "name": "은 선물"},
    "COPPER":    {"ticker": "HG=F", "name": "구리 선물"},

    # 금리
    "US10Y": {"ticker": "^TNX", "name": "미국 10년물 금리(×10)"},
}

FX_MAP: Dict[str, Dict[str, str]] = {
    "USD_KRW": {"ticker": TICKER_USD_KRW, "name": "달러/원"},
    "JPY_KRW": {"ticker": TICKER_JPY_KRW, "name": "엔/원"},
    "EUR_USD": {"ticker": TICKER_EUR_USD, "name": "유로/달러"},
    "CNY_KRW": {"ticker": "CNYKRW=X",     "name": "위안/원"},
    "EUR_KRW": {"ticker": "EURKRW=X",     "name": "유로/원"},
    "JPY_USD": {"ticker": "JPYUSD=X",     "name": "엔/달러"},
    "GBP_USD": {"ticker": "GBPUSD=X",     "name": "파운드/달러"},
    "AUD_USD": {"ticker": "AUDUSD=X",     "name": "호주달러/미달러"},
    "USD_JPY": {"ticker": "USDJPY=X",     "name": "달러/엔"},
    "USD_CNY": {"ticker": "USDCNY=X", "name": "달러/위안"},
}

def _round_or_none(v, nd=2):
    # float 변환 + 반올림, 실패 시 None
    try: return round(float(v), nd)
    except Exception: return None

def _normalize_ticker(t: str) -> str:
    # Yahoo 클래스주 표기 보정 (BRK.B → BRK-B)
    if "." in t and t.upper().split(".")[-1] in ("A","B","C","D","E","F"):
        return t.replace(".", "-")
    return t

# ===== 환율 Fallback API (yfinance 실패 시 사용) =====
def _fetch_exchange_rate_fallback(ticker: str) -> Dict[str, Any]:
    """exchangerate-api.com을 이용한 환율 조회 (yfinance 대체용)"""
    try:
        # 티커에서 통화쌍 추출 (예: USDKRW=X → USD, KRW)
        pair = ticker.replace("=X", "").upper()

        # 지원하는 통화쌍 매핑
        currency_map = {
            "USDKRW": ("USD", "KRW"),
            "JPYKRW": ("JPY", "KRW"),
            "EURUSD": ("EUR", "USD"),
            "EURKRW": ("EUR", "KRW"),
            "GBPUSD": ("GBP", "USD"),
        }

        if pair not in currency_map:
            return {"error": f"지원하지 않는 환율: {ticker}"}

        base, target = currency_map[pair]

        # exchangerate-api.com (무료, API 키 불필요)
        url = f"https://open.er-api.com/v6/latest/{base}"
        r = requests.get(url, timeout=10)
        data = r.json()

        if data.get("result") != "success":
            return {"error": "환율 API 응답 오류"}

        rate = data["rates"].get(target)
        if rate is None:
            return {"error": f"{target} 환율 데이터 없음"}

        return {
            "ticker": ticker,
            "price": round(float(rate), 2),
            "prevClose": None,
            "change": None,
            "changePct": None,
            "ts_kst": datetime.now(KST).isoformat(),
            "_source": "exchangerate-api"
        }
    except Exception as e:
        log.error("환율 fallback API 실패", ticker=ticker, error=str(e))
        return {"error": f"환율 조회 실패: {str(e)}"}

def fetch_quote_yf(ticker: str) -> Dict[str, Any]:
    # yfinance 히스토리 조회 → 현재가/전일비/등락률/기준시각(KST) 계산
    tkr = _normalize_ticker(ticker)
    price = prev_close = change = change_pct = None
    last_ts_kst = None

    def _try_hist(period, interval):
        try:
            hist = yf.Ticker(tkr).history(period=period, interval=interval, auto_adjust=False)
            if hist is not None and "Close" in hist.columns:
                return hist.dropna(subset=["Close"])
        except Exception:
            return pd.DataFrame()
        return pd.DataFrame()

    # 1분봉 우선, 부족 시 5일/일봉 보완
    df1 = _try_hist("1d", "1m")
    # fallback 5일/일봉
    if df1.empty or len(df1) < 2:
        dfd = _try_hist("5d", "1d")
    else:
        dfd = pd.DataFrame()

    # 가격/전일가/시각 산출
    if not df1.empty:
        price = float(df1["Close"].iloc[-1])
        if len(df1) >= 2:
            prev_close = float(df1["Close"].iloc[-2])
        # 기준 시각(KST)
        try:
            last_ts_kst = df1.index.tz_convert("Asia/Seoul")[-1].isoformat()
        except Exception:
            last_ts_kst = None
    elif not dfd.empty:
        price = float(dfd["Close"].iloc[-1])
        if len(dfd) >= 2:
            prev_close = float(dfd["Close"].iloc[-2])
        try:
            last_ts_kst = dfd.index.tz_convert("Asia/Seoul")[-1].isoformat()
        except Exception:
            last_ts_kst = None

    if price is not None and prev_close not in (None, 0):
        change = price - prev_close
        change_pct = (change / prev_close) * 100.0

    # 가격을 가져오지 못한 경우
    if price is None:
        # 환율 티커인 경우 fallback API 시도
        if "=" in tkr:
            log.info("yfinance 환율 실패, fallback API 시도", ticker=tkr)
            fallback_result = _fetch_exchange_rate_fallback(tkr)
            if "error" not in fallback_result:
                return fallback_result
            # fallback도 실패
            return {"ticker": tkr, "price": None, "error": f"{tkr} 환율 데이터를 가져올 수 없습니다. 잠시 후 다시 시도해주세요."}
        # 일반 주식
        return {"ticker": tkr, "price": None, "error": f"{tkr} 시세를 가져올 수 없습니다. 티커가 올바른지 확인해주세요."}

    return {
        "ticker": tkr,
        "price": _round_or_none(price, 2),
        "prevClose": _round_or_none(prev_close, 2),
        "change": _round_or_none(change, 2),
        "changePct": _round_or_none(change_pct, 2),
        "ts_kst": last_ts_kst or datetime.now(KST).isoformat()
    }
    
# 한국 지수 티커 매핑 (yFinance → PyKRX)
KRX_INDEX_MAP = {
    "^KS11": "1001",   # 코스피
    "^KQ11": "2001",   # 코스닥
}

def fetch_quote_krx_index(ticker: str) -> Dict[str, Any]:
    """PyKRX로 한국 지수 조회 (코스피, 코스닥)"""
    try:
        krx_code = KRX_INDEX_MAP.get(ticker)
        if not krx_code:
            return {"ticker": ticker, "price": None, "error": "지원하지 않는 지수"}

        today = datetime.now(KST).strftime("%Y%m%d")
        fromdate = (datetime.now(KST) - timedelta(days=7)).strftime("%Y%m%d")

        # 지수 OHLCV 조회
        df = stock.get_index_ohlcv_by_date(fromdate, today, krx_code)

        if df.empty:
            return {"ticker": ticker, "price": None, "error": ERR_NO_DATA}

        latest = df.iloc[-1]
        price = float(latest["종가"])

        prev_close = None
        change = None
        change_pct = None

        if len(df) >= 2:
            prev = df.iloc[-2]
            prev_close = float(prev["종가"])
            change = price - prev_close
            change_pct = (change / prev_close) * 100.0

        return {
            "ticker": ticker,
            "price": round(price, 2),
            "prevClose": round(prev_close, 2) if prev_close else None,
            "change": round(change, 2) if change else None,
            "changePct": round(change_pct, 2) if change_pct else None,
            "ts_kst": datetime.now(KST).isoformat()
        }

    except Exception as e:
        log.warning("PyKRX 지수 조회 실패", ticker=ticker, error=str(e))
        return {"ticker": ticker, "price": None, "error": str(e)}

def fetch_quote_krx(ticker: str) -> Dict[str, Any]:
    """PyKRX로 한국 주식 조회 (yfinance 대체)"""
    try:
        # 티커 정규화 (005930.KS → 005930)
        code = ticker.replace(".KS", "").replace(".KQ", "")

        # 오늘 날짜
        today = datetime.now(KST).strftime("%Y%m%d")

        # 최근 2일 데이터 조회 (전일 비교용)
        df = stock.get_market_ohlcv_by_date(
            fromdate=(datetime.now(KST) - timedelta(days=5)).strftime("%Y%m%d"),
            todate=today,
            ticker=code
        )

        if df.empty:
            return {"ticker": ticker, "price": None, "error": ERR_NO_DATA}

        # 최신 데이터
        latest = df.iloc[-1]
        price = float(latest["종가"])

        # 전일 데이터 (있으면)
        prev_close = None
        change = None
        change_pct = None

        if len(df) >= 2:
            prev = df.iloc[-2]
            prev_close = float(prev["종가"])
            change = price - prev_close
            change_pct = (change / prev_close) * 100.0

        return {
            "ticker": ticker,
            "price": round(price, 0),  # 원화는 소수점 없음
            "prevClose": round(prev_close, 0) if prev_close else None,
            "change": round(change, 0) if change else None,
            "changePct": round(change_pct, 2) if change_pct else None,
            "ts_kst": datetime.now(KST).isoformat()
        }

    except Exception as e:
        log.error("PyKRX 조회 실패", ticker=ticker, error=str(e))
        return {"ticker": ticker, "price": None, "error": str(e)}

# ===== 비동기 시세 조회 함수 =====
async def fetch_quote_yf_async(ticker: str) -> Dict[str, Any]:
    """yfinance 비동기 래핑 (세마포어 + ThreadPool + 딜레이)"""
    async with YF_SEMAPHORE:
        # rate limit 회피를 위한 요청 간 딜레이
        await asyncio.sleep(YF_REQUEST_DELAY)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(EXECUTOR, fetch_quote_yf, ticker)

async def fetch_quote_krx_async(ticker: str) -> Dict[str, Any]:
    """PyKRX 비동기 래핑 (세마포어 + ThreadPool)"""
    async with KRX_SEMAPHORE:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(EXECUTOR, fetch_quote_krx, ticker)

async def fetch_quote_krx_index_async(ticker: str) -> Dict[str, Any]:
    """PyKRX 지수 비동기 래핑 (세마포어 + ThreadPool)"""
    async with KRX_SEMAPHORE:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(EXECUTOR, fetch_quote_krx_index, ticker)

async def fetch_quote_cached_async(ticker: str) -> Dict[str, Any]:
    """TTL 캐시 기반 시세 조회 (비동기)"""
    now = datetime.now(KST)

    # 캐시 확인
    async with QUOTE_CACHE_LOCK:
        if ticker in QUOTE_CACHE:
            cached = QUOTE_CACHE[ticker]
            if cached["expires_at"] > now:
                log.debug("캐시 히트", ticker=ticker)
                return cached["data"]

    # 캐시 미스 → API 호출
    is_korean_index = ticker in KRX_INDEX_MAP  # ^KS11, ^KQ11
    is_korean_stock = ticker.endswith((".KS", ".KQ")) or (ticker.isdigit() and len(ticker) == 6)

    if is_korean_index:
        # 한국 지수: PyKRX 우선
        data = await fetch_quote_krx_index_async(ticker)
        if data.get("error"):
            # fallback to yfinance
            data = await fetch_quote_yf_async(ticker)
    elif is_korean_stock:
        # 한국 주식: PyKRX 우선
        data = await fetch_quote_krx_async(ticker)
        if data.get("error"):
            # fallback to yfinance
            data = await fetch_quote_yf_async(ticker)
    else:
        # 해외 주식/지수: yfinance
        data = await fetch_quote_yf_async(ticker)

    # 캐시 저장
    async with QUOTE_CACHE_LOCK:
        QUOTE_CACHE[ticker] = {
            "data": data,
            "expires_at": now + QUOTE_CACHE_TTL
        }

    return data

async def fetch_quotes_sequential(tickers: List[str], delay: float = 0.0) -> Dict[str, Dict[str, Any]]:
    """여러 티커 순차 조회 (rate limit 회피)"""
    output = {}
    for i, ticker in enumerate(tickers):
        try:
            result = await fetch_quote_cached_async(ticker)
            output[ticker] = result
        except Exception as e:
            output[ticker] = {"ticker": ticker, "error": str(e)}

        # 마지막 티커가 아니면 딜레이
        if delay > 0 and i < len(tickers) - 1:
            await asyncio.sleep(delay)
    return output

async def fetch_quotes_parallel(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
    """여러 티커 병렬 조회 (캐시 히트 시에만 효율적)"""
    tasks = [fetch_quote_cached_async(t) for t in tickers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    output = {}
    for ticker, result in zip(tickers, results):
        if isinstance(result, Exception):
            output[ticker] = {"ticker": ticker, "error": str(result)}
        else:
            output[ticker] = result
    return output

# ===== 백그라운드 캐시 갱신 =====
async def refresh_cache_background():
    """주요 시세 백그라운드 갱신 (PyKRX 기반 한국 데이터만 - yFinance rate limit 회피)"""
    # 한국 데이터만 캐싱 (PyKRX 사용, 안정적)
    # 해외 데이터(yFinance)는 사용자 요청 시에만 조회
    kr_tickers = [
        # 한국 지수 (PyKRX)
        TICKER_KOSPI, TICKER_KOSDAQ,
        # 한국 대형주 (상위 5개, PyKRX)
        "005930.KS", "000660.KS", "035420.KS", "005380.KS", "035720.KS",
    ]

    while True:
        try:
            # 순차 처리: 각 티커 간 딜레이
            await fetch_quotes_sequential(kr_tickers, delay=0.5)
            log.debug("백그라운드 캐시 갱신 완료 (한국)", ticker_count=len(kr_tickers))
        except Exception as e:
            log.warning("백그라운드 캐시 갱신 실패", error=str(e))

        await asyncio.sleep(CACHE_REFRESH_INTERVAL)

def get_market_indices() -> str:
    """주요 지수 동기 조회"""
    results = []
    for key, info in INDEX_MAP.items():
        q = fetch_quote_yf_with_cache(info["ticker"])  # 캐싱 버전 사용
        name, price, pct = info["name"], q.get("price"), q.get("changePct")
        if price is not None:
            if pct is not None:
                sign = "+" if pct >= 0 else ""
                results.append(f"**{name}**: {price:,.2f} ({sign}{pct:.2f}%)")
            else:
                results.append(f"**{name}**: {price:,.2f}")
        else:
            results.append(f"**{name}**: 데이터 없음")
    return "**주요 지수 (실시간)**\n" + "\n".join(results)

def get_fx_rates() -> str:
    # 주요 환율 요약 문자열 생성
    results = []
    for key, info in FX_MAP.items():
        q = fetch_quote_yf(info["ticker"])
        name, price, pct = info["name"], q.get("price"), q.get("changePct")
        if price is not None:
            if pct is not None:
                sign = "+" if pct >= 0 else ""
                results.append(f"• **{name}**: {price:,.2f} ({sign}{pct:.2f}%)")
            else:
                results.append(f"• **{name}**: {price:,.2f}")
        else:
            results.append(f"• **{name}**: 데이터 없음")
    return "**주요 환율 (실시간)**\n" + "\n".join(results)

# ===== 통합 시세 포맷 함수 (중복 제거) =====
def _format_single_quote(name: str, ticker: str, quote_type: str = "index", unit: str = "", multiply: int = 1) -> str:
    """
    통합 시세 포맷팅 함수 (타입 안전)
    - quote_type: "index" (지수), "fx" (환율)
    - unit: 단위 (원, 달러 등)
    - multiply: 표시 배율 (엔/원은 100배)
    """
    q = fetch_quote_yf_with_cache(ticker)
    price = q.get("price")

    if price is None:
        return f"**{name}**\n• 현재 데이터를 가져올 수 없습니다."

    # 배율 적용 (Optional 안전 처리)
    ch = _get_safe_float(q, "change")
    pct = _get_safe_float(q, "changePct")
    display_price = price * multiply if multiply > 1 else price
    display_ch = ch * multiply
    sign = "+" if display_ch >= 0 else ""

    if quote_type == "index":
        return f"**{name} (실시간)**\n• 현재가: {display_price:,.2f}\n• 변동: {sign}{display_ch:.2f} ({sign}{pct:.2f}%)"
    else:  # fx
        return f"**{name} (실시간)**\n• 현재: {display_price:,.2f}{unit}\n• 변동: {sign}{display_ch:.2f}{unit} ({sign}{pct:.2f}%)"

def get_kospi_index() -> str:
    return _format_single_quote("코스피 지수", TICKER_KOSPI, "index")

def get_kosdaq_index() -> str:
    return _format_single_quote("코스닥 지수", TICKER_KOSDAQ, "index")

def get_usd_krw() -> str:
    return _format_single_quote("원/달러 환율", TICKER_USD_KRW, "fx", "원")

def get_jpy_krw() -> str:
    return _format_single_quote("원/엔 환율", TICKER_JPY_KRW, "fx", "원", multiply=JPY_MULTIPLY)

def get_eur_usd() -> str:
    return _format_single_quote("유로/달러 환율", TICKER_EUR_USD, "fx", "달러")

@lru_cache(maxsize=LRU_CACHE_SIZE)
def _cached_fetch_quote_yf(ticker: str, cache_key: str) -> Dict[str, Any]:
    return fetch_quote_yf(ticker)

def fetch_quote_yf_with_cache(ticker: str) -> Dict[str, Any]:
    # 5분 단위로 캐시 키 생성
    cache_key = datetime.now().strftime("%Y%m%d%H%M")[:-1]  # 마지막 자리 제거
    return _cached_fetch_quote_yf(ticker, cache_key)

# ===== 뉴스 크롤러 스케줄러 =====
# 네이버 크롤러 주기 실행 (10분) 테스트 후, 1시간 간격
scheduler = BackgroundScheduler(timezone=KST)

def _job_naver():
    try:
        log.info("네이버 뉴스 크롤링 시작", limit=CRAWLER_LIMIT_PER_RUN)
        crawl_today(limit_per_run=CRAWLER_LIMIT_PER_RUN)
        log.info("네이버 뉴스 크롤링 완료")
    except Exception as e:
        log.exception("크롤링 실패", error=str(e))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ===== Startup =====
    # MongoDB 인덱스 생성
    try:
        _ensure_indexes()
        log.info("MongoDB 인덱스 생성 완료", db=settings.mongo_db_name)
    except Exception:
        log.exception("인덱스 생성 실패", db=settings.mongo_db_name)

    # 즉시 첫 크롤링 실행
    _job_naver()

    # 스케줄러 시작
    try:
        scheduler.add_job(
            _job_naver,
            "interval",
            minutes=CRAWLER_INTERVAL_MINUTES,
            id="naver_hourly",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_TIME,
        )
        scheduler.start()
        log.info("APScheduler 시작됨", interval_minutes=CRAWLER_INTERVAL_MINUTES)
    except Exception:
        log.exception("APScheduler 시작 실패")

    # 백그라운드 캐시 갱신 태스크 시작
    cache_task = asyncio.create_task(refresh_cache_background())
    log.info("백그라운드 캐시 갱신 태스크 시작", refresh_interval=CACHE_REFRESH_INTERVAL)

    yield

    # ===== Shutdown =====
    # 백그라운드 태스크 취소
    cache_task.cancel()
    with suppress(asyncio.CancelledError):
        await cache_task
    log.info("백그라운드 캐시 태스크 종료")

    try:
        scheduler.shutdown()
        log.info("APScheduler 종료됨")
    except Exception:
        log.exception("APScheduler 종료 실패")

    # httpx 클라이언트 종료 (HttpClientManager 사용)
    await http_manager.close()
    log.info("httpx 클라이언트 종료")

    # MongoDB 클라이언트 종료 (MongoClientManager 사용)
    mongo_manager.close()
    log.info("MongoDB 클라이언트 종료")

    # ThreadPoolExecutor 종료
    EXECUTOR.shutdown(wait=False)
    log.info("ThreadPoolExecutor 종료", max_workers=MAX_THREAD_WORKERS)

# ===== FastAPI 앱/CORS =====
# 앱 인스턴스 생성, 전역 CORS 허용(데모 편의)
app = FastAPI(
    title="Chat+RAG+News+Indicators (Function Calling)",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,  # Pydantic Settings에서 로드
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== 로깅 컨텍스트 미들웨어 =====
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
import time

class LoggingContextMiddleware(BaseHTTPMiddleware):
    """요청별 로깅 컨텍스트 설정 미들웨어"""

    async def dispatch(self, request: Request, call_next):
        # 고유 request_id 생성
        req_id = str(uuid.uuid4())[:8]
        request_id_var.set(req_id)

        # 요청 시작 로깅
        start_time = time.perf_counter()
        log.info(
            "요청 시작",
            method=request.method,
            path=request.url.path,
            client=request.client.host if request.client else "-"
        )

        try:
            response = await call_next(request)

            # 요청 완료 로깅
            duration_ms = (time.perf_counter() - start_time) * 1000
            log.info(
                "요청 완료",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round(duration_ms, 2)
            )
            return response

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            log.exception(
                "요청 실패",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration_ms, 2),
                error=str(e)
            )
            raise

# 미들웨어 추가
app.add_middleware(LoggingContextMiddleware)

# ===== 메인 챗 엔드포인트 =====
# 사용자 메시지 → Ollama → (필요시) 함수 호출 → 최종 답변
@app.post("/api/chat", response_model=ChatResponse)
@app.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest):
    user_msg = payload.message.strip()
    session_id = payload.session_id

    # 세션 ID를 로깅 컨텍스트에 설정
    session_id_var.set(session_id)

    if not user_msg:
        log.warning("빈 메시지 요청")
        return ChatResponse(answer="질문이 비어있습니다.", session_id=session_id)

    log.info("채팅 요청", message_length=len(user_msg))

    # "뉴스 최신/Top N" 빠른 경로 처리
    m = re.search(r"top\s*(\d{1,2})", user_msg, flags=re.IGNORECASE)
    if "뉴스" in user_msg and ("최신" in user_msg or m):
        try:
            n = max(MIN_NEWS_COUNT, min(MAX_NEWS_COUNT, int(m.group(1)))) if m else DEFAULT_NEWS_COUNT
            rows = fetch_latest_topn_from_mongo(n)
            log.info("뉴스 빠른 경로 처리", news_count=n)
            return ChatResponse(answer=format_topn_md(rows), session_id=session_id)
        except Exception:
            log.exception("뉴스 조회 실패")
            return ChatResponse(answer="DB 조회 오류. 잠시 후 다시 시도해 주세요.", session_id=session_id)

    # 세션 히스토리 구성 및 LangChain 메시지 구조화
    msgs = [{"role": "system", "content": SYSTEM_INSTRUCTIONS}]
    for t in get_session(session_id):
        msgs.append({"role": t["role"], "content": t["content"]})
    msgs.append({"role": "user", "content": user_msg})

    try:
        # 비동기 버전 사용 (캐시 + 세마포어 적용)
        agent_answer = await chat_with_agent_async(user_msg, session_id)
        log.info("채팅 응답 완료", answer_length=len(agent_answer))
        return ChatResponse(answer=agent_answer, session_id=session_id)
    except Exception:
        log.exception("채팅 처리 실패", user_message=user_msg[:100])
        return ChatResponse(answer="일시적 오류가 발생했습니다. 잠시 후 다시 시도해주세요.", session_id=session_id, error="internal_error")

# ===== 스트리밍 챗 엔드포인트 (SSE) =====
from typing import AsyncGenerator

def _sse_event(chunk: str, done: bool = False) -> str:
    """SSE 이벤트 포맷 생성"""
    return f"data: {json.dumps({'chunk': chunk, 'done': done}, ensure_ascii=False)}\n\n"

async def stream_chat_response(user_message: str, session_id: str) -> AsyncGenerator[str, None]:
    """LLM 응답을 스트리밍으로 생성 (세마포어 + 타임아웃 + 할루시네이션 방지 적용)"""

    # 1. 인사 감지 시 즉시 반환
    if any(kw in user_message.lower() for kw in GREETING_KEYWORDS):
        add_turn(session_id, "user", user_message)
        add_turn(session_id, "assistant", GREETING_RESPONSE)
        yield _sse_event(GREETING_RESPONSE, done=True)
        return

    valid_numbers = set()  # 도구 결과의 유효 숫자 (검증용)

    try:
        # 2. 규칙 기반 라우팅으로 도구 선택 (복합 질문 지원)
        route_results = router.route_multiple(user_message)

        if route_results:
            # 여러 도구 실행 및 결과 합치기
            all_tool_outputs = []

            for route_result in route_results:
                tool_name = route_result['tool']
                params = route_result['params']
                log.info("스트리밍 도구 호출", tool=tool_name, params=params)

                tool_result = await _execute_tool_async(tool_name, params)

                # 에러는 로깅만 하고 계속 진행
                if "error" in tool_result:
                    log.warning("도구 실행 실패", tool=tool_name, error=tool_result['error'])
                    all_tool_outputs.append(f"[{tool_name}] 조회 실패: {tool_result['error']}")
                    continue

                # 3. 도구 결과 검증 및 정제
                validated_result = _validate_tool_result(tool_result)
                if "error" in validated_result:
                    log.warning("도구 결과 검증 실패", tool=tool_name, error=validated_result['error'])
                    all_tool_outputs.append(f"[{tool_name}] 검증 실패: {validated_result['error']}")
                    continue

                tool_output = validated_result.get("output", "")
                if tool_output:
                    all_tool_outputs.append(f"[{tool_name}] {tool_output}")

                if "_valid_numbers" in validated_result:
                    valid_numbers.update(validated_result["_valid_numbers"])

            # 모든 도구가 실패한 경우
            if not all_tool_outputs:
                error_msg = "죄송합니다. 요청하신 정보를 조회하지 못했습니다."
                add_turn(session_id, "user", user_message)
                add_turn(session_id, "assistant", error_msg)
                yield _sse_event(error_msg, done=True)
                return

            combined_output = "\n".join(all_tool_outputs)
            prompt = _build_tool_prompt(user_message, combined_output)
        else:
            # 4. 일반 대화 (도구 없이 Gemma 2만 사용)
            history = get_session(session_id)
            prompt = _build_chat_prompt(history, user_message)

        # 5. 스트리밍 응답 생성 (세마포어 + 타임아웃 적용)
        full_response = ""
        async with OLLAMA_SEMAPHORE:
            try:
                async with asyncio.timeout(LLM_TIMEOUT_SECONDS):
                    async for chunk in llm_stream.astream(prompt):
                        if hasattr(chunk, "content") and chunk.content:
                            full_response += chunk.content
                            yield _sse_event(chunk.content, done=False)
            except asyncio.TimeoutError:
                log.warning("스트리밍 LLM 타임아웃", timeout_seconds=LLM_TIMEOUT_SECONDS)
                yield _sse_event(f"\n\n[응답 생성 시간 초과 ({LLM_TIMEOUT_SECONDS}초)]", done=True)
                return

        # 6. 응답 후처리 필터 (스트리밍 완료 후 검증)
        filtered_response = _filter_response(full_response, valid_numbers)

        # 필터링으로 변경된 경우 경고 로그
        if filtered_response != full_response:
            log.info("스트리밍 응답 필터링 적용됨", original_len=len(full_response), filtered_len=len(filtered_response))

        # 7. 세션 저장 (필터링된 버전)
        add_turn(session_id, "user", user_message)
        add_turn(session_id, "assistant", filtered_response)
        yield _sse_event("", done=True)

    except Exception as e:
        log.exception("스트리밍 채팅 처리 실패", user_message=user_message[:100])
        yield _sse_event(f"죄송합니다. 오류가 발생했습니다: {str(e)}", done=True)

@app.post("/api/chat/stream")
@app.post("/chat/stream")
async def chat_stream(payload: dict = Body(...)):
    """SSE 스트리밍 챗 엔드포인트"""
    user_msg = (payload.get("message") or "").strip()
    session_id = payload.get("session_id", "default")

    if not user_msg:
        return JSONResponse({"error": "질문이 비어있습니다."}, status_code=400)

    return StreamingResponse(
        stream_chat_response(user_msg, session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx 버퍼링 비활성화
        }
    )

# ===== 보조 시세 API =====
# 지수/환율 묶음 조회(경량 JSON)
@app.get("/api/markets")
def api_markets(indices: int = 0, fx: int = 0):
    payload = {"ts_kst": datetime.now(KST).isoformat(), "data": {}}
    if indices:
        payload["data"]["indices"] = [{"name": v["name"], **fetch_quote_yf(v["ticker"])} for v in INDEX_MAP.values()]
    if fx:
        payload["data"]["fx"] = [{"name": v["name"], **fetch_quote_yf(v["ticker"])} for v in FX_MAP.values()]
    return payload

# =========================
# S T T (CLOVA + ffmpeg)
# =========================

# ===== FFmpeg =====
# 입력 오디오 → mono/16k wav 변환 (Pydantic Settings에서 로드)
FFMPEG = settings.ffmpeg_bin

def _ffmpeg_to_wav16k(in_path: str) -> str:
    if not os.path.exists(FFMPEG):
        raise RuntimeError(f"ffmpeg not found: {FFMPEG}")
    out_path = in_path + ".wav"
    cp = subprocess.run(
        [FFMPEG, "-y", "-i", in_path, "-ac", "1", "-ar", "16000", out_path],
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        raise RuntimeError(f"ffmpeg 실패: {cp.stderr[:300]}")
    return out_path

# ===== CLOVA STT =====
# API 키/엔드포인트/언어 매핑 (Pydantic Settings에서 로드)
CLOVA_KEY_ID = settings.clova_key_id
CLOVA_KEY = settings.clova_key
CSR_URL = "https://naveropenapi.apigw.ntruss.com/recog/v1/stt"
LANG_MAP = {"ko": "Kor", "en": "Eng", "ja": "Jpn"}

def normalize_lang(l: str) -> str:
    # "ko-KR" → "Kor" 등 간단 정규화
    if not l:
        return "Kor"
    if l.lower() in ("kor", "eng", "jpn"):
        return l.title()
    return LANG_MAP.get(l.split("-")[0].lower(), "Kor")

# 업로드 파일 STT 처리 → 텍스트 반환
@app.post("/api/stt")
async def stt_clova(audio_file: UploadFile = File(...), lang: str = Query("Kor")):
    lang = normalize_lang(lang)
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=os.path.splitext(audio_file.filename or "")[1]
    ) as tmp:
        raw = await audio_file.read()
        tmp.write(raw)
        src_path = tmp.name
    wav_path = None
    try:
        wav_path = _ffmpeg_to_wav16k(src_path)
        headers = {
            "X-NCP-APIGW-API-KEY-ID": CLOVA_KEY_ID,
            "X-NCP-APIGW-API-KEY": CLOVA_KEY,
            "Content-Type": "application/octet-stream",
        }
        url = f"{CSR_URL}?lang={lang}"
        with open(wav_path, "rb") as f:
            res = requests.post(url, headers=headers, data=f.read(), timeout=STT_TIMEOUT_SECONDS)
        if res.status_code != 200:
            return JSONResponse(
                {"error": f"CSR 실패: {res.status_code} {res.text}"}, status_code=500
            )
        return {"text": res.text.strip(), "lang": lang}
    except Exception as e:
        log.exception("STT 처리 오류", lang=lang)
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        for p in (src_path, wav_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

# ==============================
# T T S (Google Cloud TTS)
# ==============================

# ===== 기본 보이스 =====
# 언어코드 → 기본 보이스 맵
DEFAULT_VOICE = {
    "ko-KR": "ko-KR-Neural2-B",
    "en-US": "en-US-Neural2-C",
    "ja-JP": "ja-JP-Neural2-B",
}

def _pick_voice(lang: str, voice: Optional[str]) -> str:
    # 지정 보이스 우선, 없으면 기본값
    if voice:
        return voice
    base = (lang or "ko-KR").split(",")[0]
    return DEFAULT_VOICE.get(base, "ko-KR-Neural2-B")

# 텍스트 → 오디오 변환 (MP3/OGG_OPUS/WAV)
# (texttospeech, service_account는 상단에서 import 완료)
from google.oauth2 import service_account

@app.post("/api/tts")
def tts_google_post(payload: dict = Body(...)):
    text = (payload.get("text") or "").strip()
    lang = payload.get("lang") or "ko-KR"
    voice = payload.get("voice") or None
    fmt = payload.get("fmt") or "MP3"
    rate = float(payload.get("rate") or 1.0)
    pitch = float(payload.get("pitch") or 0.0)

    # 텍스트 정리
    text = html.unescape(text)
    text = re.sub(r'<br\s*/?>', ' ', text)
    text = re.sub(r'<[^>]+>', '', text)   
    text = text.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
    text = text.replace('\'', '').replace('"', '').replace('…', '').replace('·', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)

    # ===== GCP 인증 강화 (Pydantic Settings에서 로드) =====
    GCP_KEY_PATH = settings.google_application_credentials
    if not GCP_KEY_PATH or not os.path.exists(GCP_KEY_PATH):
        return JSONResponse({"error": f"GCP 키 없음: {GCP_KEY_PATH}"}, status_code=400)

    try:
        # 1. 자격 증명 생성
        gcp_credentials = service_account.Credentials.from_service_account_file(GCP_KEY_PATH)
        print(f"자격 증명 생성: {gcp_credentials.project_id}")
        
        # 2. TTS 클라이언트 초기화 (TTS 스코프 명시)
        tts_client = texttospeech.TextToSpeechClient(credentials=gcp_credentials)
        
        # 3. 클라이언트 테스트 (간단한 요청)
        print("TTS 클라이언트 연결 테스트")
        
    except Exception as e:
        log.error("GCP 초기화 실패", error=str(e))
        return JSONResponse({"error": f"GCP 초기화 실패: {str(e)}"}, status_code=500)

    # ===== TTS 요청 =====
    try:
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice_name = _pick_voice(lang, voice)
        voice_params = texttospeech.VoiceSelectionParams(language_code=lang, name=voice_name)

        if fmt == "MP3":
            audio_cfg = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3, speaking_rate=rate, pitch=pitch
            )
            media_type, ext = "audio/mpeg", "mp3"
        elif fmt == "OGG_OPUS":
            audio_cfg = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.OGG_OPUS, speaking_rate=rate, pitch=pitch
            )
            media_type, ext = "audio/ogg", "ogg"
        else:
            audio_cfg = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16, speaking_rate=rate, pitch=pitch
            )
            media_type, ext = "audio/wav", "wav"

        resp = tts_client.synthesize_speech(input=synthesis_input, voice=voice_params, audio_config=audio_cfg)
        headers = {
            "Content-Type": media_type,
            "Cache-Control": "no-cache",
            "Content-Disposition": f'inline; filename="speech.{ext}"',
        }
        return StreamingResponse(io.BytesIO(resp.audio_content), headers=headers)
        
    except Exception as e:
        log.exception("Google TTS 실패", lang=lang, text_length=len(text))
        return JSONResponse({"error": f"TTS 실패: {str(e)}"}, status_code=500)

# =========================
# 유틸/헬스체크 API
# =========================

# ===== 세션 리셋 =====
# 인메모리 세션 전체 초기화
class ResetResponse(BaseModel):
    """리셋 응답 모델"""
    status: str = Field(default="ok")
    message: str = Field(default="대화 기록 초기화 완료")

@app.post("/reset", response_model=ResetResponse)
@app.post("/api/reset", response_model=ResetResponse)
async def reset(payload: Optional[ResetRequest] = None):
    session_id = payload.session_id if payload else None
    session_manager.clear(session_id)  # SessionManager로 세션 초기화 (Thread-Safe)
    msg = f"세션 '{session_id}' 초기화 완료" if session_id else "전체 대화 기록 초기화 완료"
    return ResetResponse(status="ok", message=msg)

# ===== 헬스체크 =====
# 간단 상태/서버시각(KST) 반환
@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", ts_kst=datetime.now(KST).isoformat())