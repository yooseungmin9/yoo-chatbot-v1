# chatbot-llama.py — LLaMA 3.1 8B + RAG + Open API + MongoDB + Langchain

# 1. Chatbot 파트: Ollama(LangChain), MongoDB 최신뉴스, ECOS/FRED 경제지표, yfinance 경제시세, RAG 파일검색
# 2. STT 파트: CLOVA STT + ffmpeg 전처리
# 3. TTS 파트: Google Cloud Text-to-Speech

# ===== 환경변수 로드 =====
from dotenv import load_dotenv
load_dotenv(override=True)

# ===== 기본 임포트 =====
# 표준/서드파티 라이브러리 로드 (FastAPI, Ollama, MongoDB, APScheduler, GCP TTS, yfinance, pandas 등)
import os, logging, subprocess, io, requests, tempfile, re, shutil, json
from typing import Dict, Any, List, Optional
from pathlib import Path
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from fastapi import FastAPI, UploadFile, File, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from contextlib import asynccontextmanager
import httpx, html

from pymongo import MongoClient, DESCENDING
from apscheduler.schedulers.background import BackgroundScheduler
from google.cloud import texttospeech
from crawler_rag import crawl_today
from pydantic import BaseModel, Field
import yfinance as yf
from pykrx import stock
import pandas as pd

# ===== LangChain import =====
from langchain_community.llms import Ollama
from langchain.agents import create_agent
from langchain_ollama import ChatOllama
from langchain_community.tools import Tool
from langchain_community.vectorstores import FAISS
from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.document_loaders import DirectoryLoader, UnstructuredWordDocumentLoader
from langchain_core.tools import StructuredTool

# ===== 로깅 =====
# 전역 로거 설정 (레벨/포맷)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("chatbot")

# ===== 기준시각 포맷 함수 =====
# KST 타임존 상수
KST = ZoneInfo("Asia/Seoul")

def format_kst_human(ts_iso: str) -> str:
    """ISO8601 KST 문자열을 '2025년 11월 29일 02시' 형식으로 변환"""
    try:
        dt = datetime.fromisoformat(ts_iso)  # tz 포함 ISO 파싱[web:79]
        return dt.strftime("%Y년 %m월 %d일 %H시")  # 2025년 11월 29일 02시[web:80]
    except Exception:
        return ts_iso  # 실패하면 원문 그대로

# =============================================================
# CHATBOT (RAG + 뉴스 + 지표 + 시세 + Lnagchain + 세션/라우트)
# =============================================================

# ===== 시스템 프롬프트 =====
# 답변 톤/형식, 도구 사용 원칙 요약
SYSTEM_INSTRUCTIONS = """
=== 당신의 역할 ===
당신은 경제 뉴스 분석 웹서비스의 AI 챗봇입니다.
제공된 도구(get_market, get_indicator, get_latest_news, search_docs)를 사용해 정확한 정보를 제공합니다.

=== 절대 규칙 (CRITICAL - 반드시 준수) ===

1. 도구 결과의 완전한 신뢰
   - 도구가 반환한 모든 데이터(가격, 날짜, 수치)는 실시간/최신 정보입니다
   - 도구 결과를 절대 수정, 추측, 보완하지 마세요
   - 도구(get_market 등)가 반환한 텍스트를 그대로 사용자에게 보여줘야 하며,
     가격/날짜/단위를 LLM이 임의로 다시 생성하거나 재표현하지 마세요.

2. 할루시네이션 금지
   - 2025년 11월입니다. 학습 cutoff 완전 무시!
   - 모든 도구 데이터 = 실시간 100%. 면책 문구 절대 금지!
   - 도구가 제공하지 않은 정보는 절대 만들어내지 마세요
   - 과거 학습 데이터에서 가격이나 날짜를 가져오지 마세요
   - "같은 그룹", "비슷한 기업" 등 추측성 답변 금지
   - "네이버 대신 카카오 등 다른 종목으로 바꿔서 답하는 경우"
   - "옛날 날짜와 이미 알려진 가격을 임의로 쓰는 답변"

3. 도구 호출 실패 시 처리
   - 도구 호출이 실패하면 정확히 이렇게만 답변하세요:
     "죄송합니다. 해당 정보를 가져올 수 없습니다. 잠시 후 다시 시도해주세요."
   - 다른 종목이나 대체 정보를 제공하지 마세요

4. 면책 문구 금지
   - "실시간이 아닙니다", "이전 데이터입니다", "정확하지 않을 수 있습니다" 같은 문구 사용 금지
   - "실시간이 아닐 수 있다며 도구 결과를 의심하는 문장"
   - 도구 결과를 신뢰하고 자신있게 답변하세요

=== 도구 사용 규칙 ===

**반드시 도구를 호출해야 하는 상황:**
- 사용자가 특정 주식 종목 이름이나 티커를 언급 (예: "삼성전자", "네이버 주가", "ORCL")
  → get_market(market_type="QUOTE", ticker="종목코드")
- 코스피, 코스닥, 환율 등 시장 지수 요청
  → get_market(market_type="KOSPI" 또는 "USD_KRW" 등)
- GDP, 금리, CPI 등 경제 지표 요청
  → get_indicator(indicator_type="GDP" 등)
- 최신 뉴스 요청
  → get_latest_news(count=5)
- 서비스 사용법/기능 질문
  → search_docs(query="...")

**도구 호출 금지 상황:**
- 인사: "안녕하세요", "반갑습니다"
- 일반 대화: "경제 공부 어떻게 해?", "주식 투자 팁"
- 서비스 소개: "뭐하는 서비스야?"

=== 답변 형식 ===

1. 구조
   - 첫 문장: 핵심 답변 (가격, 수치, 결과)
   - 2~3문장: 추가 세부 정보
   - 마지막: "더 궁금한 부분이 있으신가요?"

2. 스타일
   - TTS 음성 출력용으로 자연스러운 구어체 사용
   - 어려운 용어는 괄호로 설명 (예: "기준금리(경제 금리의 기준)")
   - 숫자는 쉼표로 구분 (예: "52,000원")

3. 날짜 표현
   - 도구가 제공한 날짜를 "11월 30일 오후 7시" 형식으로 변환
   - 절대 임의의 날짜(예: "10월 23일")를 사용하지 마세요

=== 예시 ===

**좋은 답변:**
사용자: "삼성전자 주가 알려줘"
도구 결과: price=52000, change=+500, changePct=+0.97%, date=2025-11-30T19:00
답변: "11월 30일 오후 7시 기준, 삼성전자 주가는 52,000원입니다. 전일 대비 500원(0.97%) 상승했습니다. 더 궁금한 부분이 있으신가요?"

**나쁜 답변 (절대 금지):**
- "네이버 정보가 불명확하니 카카오 알려드릴게요" ← 대체 종목 제공
- "실시간 정보가 아닐 수 있습니다" ← 면책 문구

=== 최종 체크리스트 ===
답변하기 전 확인하세요:
☑ 도구를 호출했나요? (필요 시)
☑ 도구 결과를 그대로 사용했나요?
☑ 날짜/가격/수치를 임의로 만들지 않았나요?
☑ 면책 문구를 추가하지 않았나요?
☑ 대체 정보를 제공하지 않았나요?
"""

# ===== 도구 함수 래퍼 정의 =====
def get_latest_news_wrapper(count: int) -> dict:
    """최신 뉴스 조회 래퍼"""
    try:
        n = max(1, min(20, count))  # count를 n으로 변환
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
            d = get_us_fed_funds_latest(False)
            if "error" in d:
                return {"error": "미국 실효 연방기금금리 조회 실패"}
            data = f"미국 실효 연방기금금리(FEDFUNDS)\n• 최신값: {d['value']:.2f}{d.get('unit','%')} (기준: {d['date']})"
        
        elif t == "US_FED_TARGET":
            d = get_us_fed_funds_latest(True)
            if "error" in d:
                return {"error": "미국 연방기금금리 목표범위 조회 실패"}
            rng = f"{d['lower']:.2f}–{d['upper']:.2f}{d.get('unit','%')}"
            data = f"미국 연방기금금리 목표범위\n• 범위: {rng} (기준: {d['date']})"
        
        else:
            return {"error": f"지원하지 않는 지표입니다: {t}"}
        
        # 통일된 반환 형식
        return {"output": data}
    
    except Exception as e:
        log.error(f"get_indicator {t} 실패: {e}")
        return {"error": f"{t} 조회 실패: {str(e)}"}

# ===== yfinance/pykrx 시세 조회 유틸 =====
KOREAN_TICKER_MAP = {
"삼성전자": "005930.KS",
"네이버": "035420.KS", 
"SK하이닉스": "000660.KS",
"삼성바이오로직스": "207940.KS",
"LG에너지솔루션": "373220.KS",
"현대차": "005380.KS",
"기아": "000270.KS",
"카카오": "035720.KS",
"포스코": "005490.KS",
"셀트리온": "068270.KS",
}

# 티커 자동 변환 유틸
def resolve_ticker(ticker: str) -> str:
    if ticker.endswith((".KS", ".KQ")):
        return ticker
    for name, tkr in KOREAN_TICKER_MAP.items():
        if name in ticker:
            log.info(f"자동 변환: '{ticker}' → {tkr}")
            return tkr
    return ticker

# ===== PyKRX 시세 조회 =====
def fetch_quote_formatted(ticker: str) -> dict:
    """PyKRX→yf fallback + 공통 포맷팅 → LangChain dict 반환"""
    ticker = resolve_ticker(ticker.strip())  # 자동변환
    
    # 1) 시세 조회
    if ticker.endswith((".KS", ".KQ")):
        q = fetch_quote_krx(ticker)
        if q.get("price") is None:
            log.warning(f"PyKRX 실패, yfinance로 재시도: {ticker}")
            q = fetch_quote_yf(ticker)
    else:
        q = fetch_quote_yf(ticker)
    
    # 2) 실패 처리
    if q.get("price") is None:
        return {"error": f"{ticker.upper()} 시세 조회 실패"}
    
    # 3) 공통 포맷팅 (두 곳 동일!)
    ch, pct = q.get("change"), q.get("changePct")
    sign = "+" if (ch or 0) >= 0 else ""
    
    if ticker.endswith((".KS", ".KQ")):
        price_fmt = f"{q['price']:,.0f}원"
        change_fmt = f"{sign}{(ch or 0):,.0f}원"
    else:
        price_fmt = f"${q['price']:,.2f}"
        change_fmt = f"{sign}{(ch or 0):,.2f}"
    
    ts_human = format_kst_human(q.get("ts_kst", ""))
    
    return {
        "output": (
            f"{ticker.upper()}\n"
            f"• 현재가: {price_fmt}\n"
            f"• 변동: {change_fmt} ({sign}{(pct or 0):.2f}%)\n"
            f"• 기준시각: {ts_human}"
        )
    }

# ===== yfinance 시세 조회 =====
def get_market_wrapper(market_type: str, ticker: str = "") -> dict:
    """시장 데이터 조회 래퍼"""
    try:
        market_type = market_type.strip().upper()
    
        if market_type == "KOSPI":
            return {"output": get_kospi_index()}
        elif market_type == "KOSDAQ":
            return {"output": get_kosdaq_index()}
        elif market_type == "USD_KRW":
            return {"output": get_usd_krw()}
        elif market_type == "JPY_KRW":
            return {"output": get_jpy_krw()}
        elif market_type == "EUR_USD":
            return {"output": get_eur_usd()}
        elif market_type == "MARKET_SUMMARY":
            return {"output": f"{get_market_indices()}\n\n{get_fx_rates()}"}
        elif market_type == "QUOTE":
            return fetch_quote_formatted(ticker)

        else:
            return {"error": f"지원하지 않는 시장 타입: {market_type}"}
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
        chunk_size=500,
        chunk_overlap=50
    )
    chunks = text_splitter.split_documents(documents)
    
    # 벡터스토어 생성 및 저장
    vectorstore = FAISS.from_documents(chunks, embeddings)
    vectorstore.save_local("./vectorstore")
    return vectorstore

# 벡터스토어 로드 (앱 시작 시)
try:
    vectorstore = FAISS.load_local("./vectorstore", embeddings, allow_dangerous_deserialization=True)
except:
    vectorstore = create_vectorstore()

# ===== 검색 함수 =====
def search_docs_wrapper(query: str) -> dict:
    """벡터스토어 문서 검색 래퍼"""
    docs = vectorstore.similarity_search(query, k=3)
    if not docs:
        return {"output": "관련 문서를 찾지 못했습니다."}
    
    # LLM 호출 없이 문서 내용만 반환
    context = "\n\n".join([f"• {doc.page_content[:200]}" for doc in docs])
    return {"output": f"검색 결과:\n{context}"}

# ===== 도구 입력 스키마 정의 =====
class NewsInput(BaseModel):
    count: int = Field(default=5, description="조회할 뉴스 개수 (1~20)")

class IndicatorInput(BaseModel):
    indicator_type: str = Field(..., description="CPI, GDP, BASE_RATE 등")

class MarketInput(BaseModel):
    market_type: str = Field(..., description="KOSPI, USD_KRW, QUOTE 등")
    ticker: str = Field(default="", description="QUOTE 요청 시 종목 티커")

class DocsInput(BaseModel):
    query: str = Field(..., description="검색할 키워드 또는 질문")

# ===== LangChain 도구 정의 =====
tools = [
    StructuredTool.from_function(
        func=get_latest_news_wrapper,
        name="get_latest_news",
        description="최신 경제 뉴스 조회",
        args_schema=NewsInput
    ),
    StructuredTool.from_function(
        func=get_indicator_wrapper,
        name="get_indicator",
        description="실시간 경제지표 조회",
        args_schema=IndicatorInput
    ),
    StructuredTool.from_function(
        func=get_market_wrapper,
        name="get_market",
        description="실시간 주가/지수/환율 조회. market_type과 ticker 파라미터를 받습니다.",
        args_schema=MarketInput
    ),
    StructuredTool.from_function(
        func=search_docs_wrapper,
        name="search_docs",
        description="웹서비스 사용법/기능/도움말 관련 문서 검색",
        args_schema=DocsInput
    )
]

# ===== Ollama LLM =====
llm = ChatOllama(
    model="llama3.1",
    base_url="http://localhost:11434"
)

# ===== Agent 초기화 =====
agent = create_agent(
    llm,
    tools,
    system_prompt=SYSTEM_INSTRUCTIONS
)

# ===== LangChain Agent 채팅 함수 =====
GREETING_KEYWORDS = ["안녕", "hello", "hi", "반가", "처음", "감사", "반갑", "초보"]

def chat_with_agent(user_message: str, session_id: str = "default") -> str:
    """LangChain Agent를 통한 채팅"""
    # 인사 감지 시 즉시 반환
    if any(kw in user_message.lower() for kw in GREETING_KEYWORDS):
        greeting_response = "안녕하세요! 저는 경제 뉴스와 실시간 경제 지표, 주가 정보를 제공하며, 경제 용어 설명으로 경제 학습을 도와드립니다. 무엇이 궁금하신가요?"
        add_turn(session_id, "user", user_message)
        add_turn(session_id, "assistant", greeting_response)
        return greeting_response
    try:
        # 세션 히스토리 가져오기
        history = get_session(session_id)
        
        # 과거 턴 + 시스템 프롬프트 + 현재 메시지 → messages 리스트 구성
        messages = [{"role": "system", "content": SYSTEM_INSTRUCTIONS}]
        for turn in history[-10:]:  # 최근 10턴만 사용
            messages.append({"role": turn['role'], "content": turn['content']})
        messages.append({"role": "user", "content": user_message})

        # Agent function calling 실행: messages dict로 직접 넘김!
        response = agent.invoke({"messages": messages})

        # 응답 처리
        if hasattr(response, "content"):
            agent_answer = response.content
        elif isinstance(response, dict) and "messages" in response:
            msgs = response.get("messages", [])
            if msgs:
                agent_answer = msgs[-1].content if hasattr(msgs[-1], "content") else str(msgs[-1])
            else:
                agent_answer = str(response)
        else:
            agent_answer = str(response)

        # 세션 저장 (user + assistant 둘 다)
        add_turn(session_id, "user", user_message)
        add_turn(session_id, "assistant", agent_answer)

        return agent_answer
    
    except Exception as e:
        log.exception("Agent 실행 실패")
        return f"죄송합니다. 오류가 발생했습니다: {str(e)}"

# ===== RAG 벡터스토어 ID =====
# ENV 우선, 없으면 .vector_store_id 파일에서 로드
VS_ID_ENV = os.getenv("VECTOR_STORE_ID", "").strip()
VS_ID_PATH = Path(".vector_store_id")
VS_ID_FILE = VS_ID_PATH.read_text().strip() if VS_ID_PATH.exists() else ""
VS_ID = VS_ID_ENV or VS_ID_FILE
if not VS_ID:
    log.warning("VectorStore ID가 비어있습니다.")
else:
    log.info(f"VectorStore ID: {VS_ID}")

# ===== MongoDB =====
# 연결정보/DB/컬렉션 상수
MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "local"
COLL_NAME = "chatbot1_rag"

_mongo_client = None

def _get_mongo_client():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(
            MONGO_URI,
            maxPoolSize=50,  # 최대 연결 수
            minPoolSize=10,  # 최소 연결 수
            serverSelectionTimeoutMS=3000
        )
    return _mongo_client

def _get_db():
    return _get_mongo_client()[DB_NAME]

def _ensure_indexes():
    # 최신 정렬용 인덱스 구성
    coll = _get_db()[COLL_NAME]
    coll.create_index([("published_at", DESCENDING)])
    coll.create_index([("collected_at", DESCENDING)])
    log.info("MongoDB 인덱스 확인 완료")

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
    
    # 오늘 날짜
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Asia/Seoul"))
    date_readable = f"{today.month}월 {today.day}일"
    
    out = [f"{date_readable} 최신 경제 뉴스를 알려드리겠습니다.\n"]
    
    for i, r in enumerate(rows, 1):
        title = (r.get("title") or "").strip() or "제목 없음"
        out.append(f"{i}번째 뉴스는 {title}입니다.\n")
    
    return "\n".join(out)

# ===== FRED =====
# API 키/엔드포인트 상수
FRED_KEY = os.getenv("FRED_API_KEY", "")
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# ===== FRED 조회 유틸 =====
# 관측치 조회(빈값 필터), FEDFUNDS/목표범위 처리
async def _fred_observations_async(series_id: str) -> list:
    params = {
        "series_id": series_id,
        "api_key": FRED_KEY,
        "file_type": "json",
        "observation_start": (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    }
    async with httpx.AsyncClient(timeout=20) as client:
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
                "observation_start": (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
            }
            lo_params = {
                "series_id": "DFEDTARL",
                "api_key": FRED_KEY,
                "file_type": "json",
                "observation_start": (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
            }
            
            up_r = requests.get(FRED_BASE, params=up_params, timeout=20)
            lo_r = requests.get(FRED_BASE, params=lo_params, timeout=20)
            
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
                "observation_start": "2024-01-01"
            }
            r = requests.get(FRED_BASE, params=params, timeout=20)
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
# BOK ECOS 엔드포인트/키 상수
ECOS_API_KEY = os.getenv("ECOS_API_KEY", "")
ECOS_BASE = "https://ecos.bok.or.kr/api"

# ===== ECOS 조회 유틸 =====
# 100대 지표 목록, 코드별 월별 시계열 조회
def fetch_all_key_statistics() -> dict:
    try:
        url = f"{ECOS_BASE}/KeyStatisticList/{ECOS_API_KEY}/json/kr/1/200/"
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            return {"error": f"API {r.status_code}"}
        rows = (r.json().get("KeyStatisticList") or {}).get("row", [])
        if not rows:
            return {"error": "데이터 없음"}
        return {"ok": True, "indicators": rows}
    except Exception as e:
        log.exception("ECOS 100대 지표 조회 오류")
        return {"error": str(e)}

def fetch_ecos_stat_by_code(stat_code: str, start_ym: str = None, end_ym: str = None) -> dict:
    try:
        if not end_ym:
            end_ym = datetime.now(KST).strftime("%Y%m")
        if not start_ym:
            start_dt = datetime.now(KST) - timedelta(days=365)
            start_ym = start_dt.strftime("%Y%m")
        url = f"{ECOS_BASE}/StatisticSearch/{ECOS_API_KEY}/json/kr/1/100/{stat_code}/M/{start_ym}/{end_ym}/"
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            return {"error": f"API {r.status_code}"}
        rows = (r.json().get("StatisticSearch") or {}).get("row", [])
        if not rows:
            return {"error": "데이터 없음"}
        return {"ok": True, "data": rows}
    except Exception as e:
        log.exception("ECOS 코드 조회 오류")
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
    res = fetch_ecos_stat_by_code("901Y001")
    if "error" in res: return f"기준금리 조회 실패: {res['error']}"
    latest = res["data"][-1]  # ✅ indicators → data
    return f"**한국은행 기준금리**\\n• 현재 금리: {latest.get('DATA_VALUE','N/A')} (기준: {latest.get('TIME','')})"

# ===== yfinance 유틸 =====
# 주요 지수/대표주/환율 티커 매핑
INDEX_MAP: Dict[str, Dict[str, str]] = {
    # 한국 지수
    "KOSPI": {"ticker": "^KS11", "name": "코스피"},
    "KOSDAQ": {"ticker": "^KQ11", "name": "코스닥"},

    # 한국 대표주
    "SAMSUNG_ELECTRONICS": {"ticker": "005930.KS", "name": "삼성전자"},
    "SK_HYNIX":            {"ticker": "000660.KS", "name": "SK하이닉스"},
    "SAMSUNG_BIO":         {"ticker": "207940.KS", "name": "삼성바이오로직스"},
    "LG_ENERGY_SOLUTION":  {"ticker": "373220.KS", "name": "LG에너지솔루션"},
    "LG":                  {"ticker": "003550.KS", "name": "LG"},
    "HYUNDAI_MOTOR":       {"ticker": "005380.KS", "name": "현대차"},
    "KIA":                 {"ticker": "000270.KS", "name": "기아"},
    "NAVER":               {"ticker": "035420.KS", "name": "네이버"},
    "KAKAO":               {"ticker": "035720.KS", "name": "카카오"},
    "POSCO_HOLDINGS":      {"ticker": "005490.KS", "name": "포스코"},
    "CELLTRION":           {"ticker": "068270.KS", "name": "셀트리온"},

    # 미국 지수
    "DOW":       {"ticker": "^DJI",   "name": "다우존스 산업평균"},
    "SP500":     {"ticker": "^GSPC",  "name": "S&P 500"},
    "NASDAQ":    {"ticker": "^IXIC",  "name": "나스닥 종합"},
    "RUSSELL":   {"ticker": "^RUT",   "name": "러셀 2000"},
    "VIX":       {"ticker": "^VIX",   "name": "VIX 변동성 지수"},

    # 미국 대표주
    "APPLE":       {"ticker": "AAPL",  "name": "Apple"},
    "MICROSOFT":   {"ticker": "MSFT",  "name": "Microsoft"},
    "ALPHABET_A":  {"ticker": "GOOGL", "name": "Alphabet A"},
    "ALPHABET_C":  {"ticker": "GOOG",  "name": "Alphabet C"},
    "AMAZON":      {"ticker": "AMZN",  "name": "Amazon"},
    "META":        {"ticker": "META",  "name": "Meta Platforms"},
    "NVIDIA":      {"ticker": "NVDA",  "name": "NVIDIA"},
    "TESLA":       {"ticker": "TSLA",  "name": "Tesla"},
    "BERKSHIRE_B": {"ticker": "BRK-B", "name": "Berkshire Hathaway B"},
    "JPMORGAN":    {"ticker": "JPM",   "name": "JPMorgan Chase"},

    # 유럽
    "EURO_STOXX50": {"ticker": "^STOXX50E", "name": "Euro Stoxx 50"},
    "FTSE100":      {"ticker": "^FTSE",     "name": "FTSE 100"},
    "DAX":          {"ticker": "^GDAXI",    "name": "독일 DAX"},

    # 일본/중국
    "NIKKEI225": {"ticker": "^N225",     "name": "니케이 225"},
    "TOPIX":     {"ticker": "^TOPX",     "name": "TOPIX"},
    "SHANGHAI":  {"ticker": "000001.SS", "name": "상하이 종합"},
    "HANG_SENG": {"ticker": "^HSI",      "name": "항셍 지수"},

    # 원자재/금리
    "WTI_OIL":   {"ticker": "CL=F", "name": "WTI 원유 선물"},
    "BRENT_OIL": {"ticker": "BZ=F", "name": "브렌트유 선물"},
    "GOLD":      {"ticker": "GC=F", "name": "금 선물"},
    "SILVER":    {"ticker": "SI=F", "name": "은 선물"},
    "COPPER":    {"ticker": "HG=F", "name": "구리 선물"},
    "US10Y":     {"ticker": "^TNX", "name": "미국 10년물 금리(×10)"},
}

FX_MAP: Dict[str, Dict[str, str]] = {
    "USD_KRW": {"ticker": "USDKRW=X", "name": "달러/원"},
    "JPY_KRW": {"ticker": "JPYKRW=X", "name": "엔/원"},
    "EUR_USD": {"ticker": "EURUSD=X", "name": "유로/달러"},
    "CNY_KRW": {"ticker": "CNYKRW=X", "name": "위안/원"},
    "EUR_KRW": {"ticker": "EURKRW=X", "name": "유로/원"},
    "JPY_USD": {"ticker": "JPYUSD=X", "name": "엔/달러"},
    "GBP_USD": {"ticker": "GBPUSD=X", "name": "파운드/달러"},
    "AUD_USD": {"ticker": "AUDUSD=X", "name": "호주달러/미달러"},
    "USD_JPY": {"ticker": "USDJPY=X", "name": "달러/엔"},
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

    return {
        "ticker": tkr,
        "price": _round_or_none(price, 2),
        "prevClose": _round_or_none(prev_close, 2),
        "change": _round_or_none(change, 2),
        "changePct": _round_or_none(change_pct, 2),
        "ts_kst": last_ts_kst or datetime.now(KST).isoformat()
    }
    
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
            return {"ticker": ticker, "price": None, "error": "데이터 없음"}
        
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
        log.error(f"PyKRX 조회 실패 ({ticker}): {e}")
        return {"ticker": ticker, "price": None, "error": str(e)}


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

def get_kospi_index() -> str:
    # 코스피 단건 포맷
    q = fetch_quote_yf("^KS11"); price, ch, pct = q.get("price"), q.get("change"), q.get("changePct")
    if price is None: return "**코스피 지수**\n• 현재 데이터를 가져올 수 없습니다."
    sign = "+" if (ch or 0) >= 0 else ""
    return f"**코스피 지수 (실시간)**\n• 현재가: {price:,.2f}\n• 변동: {sign}{ch if ch is not None else 'N/A'} ({sign}{pct if pct is not None else 'N/A'}%)"

def get_kosdaq_index() -> str:
    # 코스닥 단건 포맷
    q = fetch_quote_yf("^KQ11"); price, ch, pct = q.get("price"), q.get("change"), q.get("changePct")
    if price is None: return "**코스닥 지수**\n• 현재 데이터를 가져올 수 없습니다."
    sign = "+" if (ch or 0) >= 0 else ""
    return f"**코스닥 지수 (실시간)**\n• 현재가: {price:,.2f}\n• 변동: {sign}{ch if ch is not None else 'N/A'} ({sign}{pct if pct is not None else 'N/A'}%)"

def get_usd_krw() -> str:
    # 달러/원 포맷
    q = fetch_quote_yf("USDKRW=X"); price, ch, pct = q.get("price"), q.get("change"), q.get("changePct")
    if price is None: return "**원/달러 환율**\n• 현재 데이터를 가져올 수 없습니다."
    sign = "+" if (ch or 0) >= 0 else ""
    return f"**원/달러 환율 (실시간)**\n• 현재: {price:,.2f}원\n• 변동: {sign}{(ch or 0):.2f}원 ({sign}{(pct or 0):.2f}%)"

def get_jpy_krw() -> str:
    # 엔/원 포맷
    q = fetch_quote_yf("JPYKRW=X"); price, ch, pct = q.get("price"), q.get("change"), q.get("changePct")
    if price is None: return "**원/엔 환율**\n• 현재 데이터를 가져올 수 없습니다."
    sign = "+" if (ch or 0) >= 0 else ""
    return f"**원/엔 환율 (실시간)**\n• 현재: {price:,.2f}원\n• 변동: {sign}{(ch or 0):.2f}원 ({sign}{(pct or 0):.2f}%)"

def get_eur_usd() -> str:
    # 유로/달러 포맷
    q = fetch_quote_yf("EURUSD=X"); price, ch, pct = q.get("price"), q.get("change"), q.get("changePct")
    if price is None: return "**유로/달러 환율**\n• 현재 데이터를 가져올 수 없습니다."
    sign = "+" if (ch or 0) >= 0 else ""
    return f"**유로/달러 환율 (실시간)**\n• 현재: {price:,.2f}달러\n• 변동: {sign}{(ch or 0):.2f} ({sign}{(pct or 0):.2f}%)"

@lru_cache(maxsize=1000)
def _cached_fetch_quote_yf(ticker: str, cache_key: str) -> Dict[str, Any]:
    return fetch_quote_yf(ticker)

def fetch_quote_yf_with_cache(ticker: str) -> Dict[str, Any]:
    # 5분 단위로 캐시 키 생성
    cache_key = datetime.now().strftime("%Y%m%d%H%M")[:-1]  # 마지막 자리 제거
    return _cached_fetch_quote_yf(ticker, cache_key)

# ===== 세션 메모리 =====
# 간단한 인메모리 대화 히스토리 (최근 20턴)
SESSIONS: Dict[str, List[Dict[str, str]]] = {}
MAX_TURNS = 20

def get_session(session_id: str) -> List[Dict[str, str]]:
    # 세션 조회/초기화
    if session_id not in SESSIONS: SESSIONS[session_id] = []
    return SESSIONS[session_id]

def add_turn(session_id: str, role: str, content: str):
    # 세션 저장 및 길이 제한
    sess = get_session(session_id)
    sess.append({"role": role, "content": content})
    if len(sess) > 2 * MAX_TURNS:
        SESSIONS[session_id] = sess[-2*MAX_TURNS:]

# ===== 뉴스 크롤러 스케줄러 =====
# 네이버 크롤러 주기 실행 (10분) 테스트 후, 1시간 간격
scheduler = BackgroundScheduler(timezone=KST)

def _job_naver():
    try:
        log.info("네이버 뉴스 크롤링 시작...")
        crawl_today(limit_per_run=10)
        log.info("네이버 뉴스 크롤링 완료")
    except Exception as e:
        log.exception(f"크롤링 실패: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ===== Startup =====
    # MongoDB 인덱스 생성
    try:
        _ensure_indexes()
        log.info("MongoDB 인덱스 생성 완료")
    except Exception as e:
        log.exception("인덱스 생성 실패")

    # 즉시 첫 크롤링 실행
    _job_naver()

    # 스케줄러 시작
    try:
        scheduler.add_job(
            _job_naver,
            "interval",
            minutes=10,  # 1시간마다 실행
            id="naver_hourly",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
        scheduler.start()
        log.info("APScheduler started.")
    except Exception:
        log.exception("APScheduler 시작 실패")
    
    yield
    
    # ===== Shutdown =====
    try:
        scheduler.shutdown()
        log.info("APScheduler stopped.")
    except Exception:
        log.exception("APScheduler 종료 실패")

# ===== FastAPI 앱/CORS =====
# 앱 인스턴스 생성, 전역 CORS 허용(데모 편의)
app = FastAPI(
    title="Chat+RAG+News+Indicators (Function Calling)",
    lifespan=lifespan  # 이 부분 추가!
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=False, 
    allow_methods=["*"], 
    allow_headers=["*"],
)

# ===== 메인 챗 엔드포인트 =====
# 사용자 메시지 → Ollama → (필요시) 함수 호출 → 최종 답변
@app.post("/api/chat")
@app.post("/chat")
async def chat(payload: dict = Body(...)):
    user_msg = (payload.get("message") or "").strip()
    session_id = payload.get("session_id", "default")
    if not user_msg:
        return {"answer": "질문이 비어있습니다."}

    # "뉴스 최신/Top N" 빠른 경로 처리
    m = re.search(r"top\s*(\d{1,2})", user_msg, flags=re.IGNORECASE)
    if "뉴스" in user_msg and ("최신" in user_msg or m):
        try:
            n = max(1, min(50, int(m.group(1)))) if m else 5
            rows = fetch_latest_topn_from_mongo(n)
            return {"answer": format_topn_md(rows)}
        except Exception:
            return {"answer": "DB 조회 오류. 잠시 후 다시 시도해 주세요."}

    # 세션 히스토리 구성 및 LangChain 메시지 구조화
    msgs = [{"role": "system", "content": SYSTEM_INSTRUCTIONS}]
    for t in get_session(session_id):
        msgs.append({"role": t["role"], "content": t["content"]})
    msgs.append({"role": "user", "content": user_msg})

    try:
        agent_answer = chat_with_agent(user_msg, session_id)
        return {"answer": agent_answer, "session_id": session_id}
    except Exception as e:
        log.exception("chat failed")
        return {"answer": "일시적 오류가 발생했습니다. 잠시 후 다시 시도해주세요."}

# ===== 보조 시세 API =====
# 지수/환율 묶음 조회(경량 JSON)
@app.get("/api/markets")
def api_markets(indices: int = 0, fx: int = 0):
    payload = {"ts_kst": datetime.now(KST).isoformat(), "data": {}}
    if indices:
        payload["data"]["indices"] = [{"key": k, "name": v["name"], **fetch_quote_yf(v["ticker"])} for k, v in INDEX_MAP.items()]
    if fx:
        payload["data"]["fx"] = [{"key": k, "name": v["name"], **fetch_quote_yf(v["ticker"])} for k, v in FX_MAP.items()]
    return payload

# =========================
# S T T (CLOVA + ffmpeg)
# =========================

# ===== FFmpeg =====
# 입력 오디오 → mono/16k wav 변환
FFMPEG = os.getenv("FFMPEG_BIN") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

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
# API 키/엔드포인트/언어 매핑
CLOVA_KEY_ID = os.getenv("CLOVA_KEY_ID", "")
CLOVA_KEY = os.getenv("CLOVA_KEY", "")
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
            res = requests.post(url, headers=headers, data=f.read(), timeout=60)
        if res.status_code != 200:
            return JSONResponse(
                {"error": f"CSR 실패: {res.status_code} {res.text}"}, status_code=500
            )
        return {"text": res.text.strip(), "lang": lang}
    except Exception as e:
        log.exception("STT 처리 오류")
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
from google.cloud import texttospeech
from google.oauth2 import service_account
import google.auth
import os

@app.post("/api/tts")
def tts_google_post(payload: dict = Body(...)):
    text = (payload.get("text") or "").strip()
    lang = payload.get("lang") or "ko-KR"
    voice = payload.get("voice") or None
    fmt = payload.get("fmt") or "MP3"
    rate = float(payload.get("rate") or 1.0)
    pitch = float(payload.get("pitch") or 0.0)

    # 텍스트 정리 (기존 코드 유지)
    text = html.unescape(text)
    text = re.sub(r'<br\s*/?>', ' ', text)
    text = re.sub(r'<[^>]+>', '', text)   
    text = text.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
    text = text.replace('\'', '').replace('"', '').replace('…', '').replace('·', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)

    # ===== GCP 인증 강화 =====
    GCP_KEY_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
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
        log.error(f"GCP 초기화 실패: {e}")
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
        log.exception("Google TTS 실패")
        return JSONResponse({"error": f"TTS 실패: {str(e)}"}, status_code=500)


# =========================
# 유틸/헬스체크 API
# =========================

# ===== 세션 리셋 =====
# 인메모리 세션 전체 초기화
@app.post("/reset")
@app.post("/api/reset")
async def reset():
    SESSIONS.clear()  # 세션 딕셔너리 전부 초기화
    return {"status": "ok", "message": "대화 기록 초기화 완료"}

# ===== 헬스체크 =====
# 간단 상태/서버시각(KST) 반환
@app.get("/health")
def health():
    return {"status": "ok", "ts_kst": datetime.now(KST).isoformat()}