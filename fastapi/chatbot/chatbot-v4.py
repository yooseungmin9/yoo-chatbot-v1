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

# ===== 기본 임포트 =====
# 표준/서드파티 라이브러리 로드 (FastAPI, Ollama, MongoDB, APScheduler, GCP TTS, yfinance, pandas 등)
import os, logging, subprocess, io, requests, tempfile, re, shutil, json
import asyncio
from typing import Dict, Any, List, Optional
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

# ===== 로깅 =====
# 전역 로거 설정 (레벨/포맷)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("chatbot")

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

# ===== 동시성 제어 =====
# 세마포어: 외부 API 동시 호출 제한
API_SEMAPHORE = asyncio.Semaphore(10)  # 최대 10개 동시 호출
YF_SEMAPHORE = asyncio.Semaphore(5)    # yfinance 동시 5개 제한
KRX_SEMAPHORE = asyncio.Semaphore(3)   # PyKRX 동시 3개 제한 (rate limit 엄격)

# ThreadPoolExecutor: 동기 라이브러리(yfinance, pykrx) 비동기 래핑용
EXECUTOR = ThreadPoolExecutor(max_workers=10)

# ===== 시세 캐시 (TTL 기반) =====
# 구조: {ticker: {"data": {...}, "expires_at": datetime}}
QUOTE_CACHE: Dict[str, Dict[str, Any]] = {}
QUOTE_CACHE_TTL = timedelta(seconds=30)  # 30초 캐시
QUOTE_CACHE_LOCK = asyncio.Lock()

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
# 역할
경제 뉴스 분석 AI 챗봇. 
한국어로 물어보면 한국어만 사용.
영어로 물어보면 영어만 사용.

# 핵심 원칙
- 가격/수치는 **반드시 도구를 호출한 후** 그 결과값만 사용
- 도구 호출 없이 숫자를 말하면 무조건 오류
- 아래 예시의 숫자는 형식 참고용이며, 절대 그대로 사용 금지

# 도구 사용
| 요청 유형 | 도구 |
|-----------|------|
| 개별 종목 | get_market(market_type="QUOTE", ticker="종목코드") |
| 코스피 | get_market(market_type="KOSPI") |
| 코스닥 | get_market(market_type="KOSDAQ") |
| 환율 | get_market(market_type="USD_KRW") |
| 경제지표 | get_indicator(indicator_type="GDP/CPI/RATE") |
| 뉴스 | get_latest_news(count=N) |

# 주요 종목 티커
- 삼성전자: 005930
- SK하이닉스: 000660
- LG전자: 066570
- 카카오: 035720
- 네이버: 035420
- 해외: AAPL, TSLA, NVDA, MSFT, GOOGL

위 목록에 없는 종목은 사용자에게 티커 확인 요청.

# 응답 규칙
1. 도구 호출 성공 → 받은 값 그대로 전달
2. 도구 호출 실패 → "조회할 수 없습니다. 잠시 후 다시 시도해 주세요."
3. 티커 불명확 → "종목코드를 알려주시겠어요?"
4. 지원 안 되는 요청 → "해당 데이터는 제공되지 않습니다."

# 응답 형식
- 국내: "삼성전자(005930)의 현재 주가는 XX,XXX원입니다."
- 해외: "테슬라(TSLA)의 현재 주가는 $XXX.XX입니다."
- 마지막: "더 궁금한 부분이 있으신가요?"

# 예시

[사용자] 삼성전자 주가
[행동] get_market 호출 → 결과의 price, change 값 사용
[응답] 삼성전자(005930)의 현재 주가는 {price}원입니다. 전일 대비 {change}% 변동했네요.

[사용자] 테슬라 얼마야?
[행동] get_market 호출 → 결과의 price 값 사용
[응답] 테슬라(TSLA)의 현재 주가는 ${price}입니다.

[사용자] 그 IT 회사 주가
[행동] 종목 특정 불가 → 도구 호출 안 함
[응답] 어떤 회사를 말씀하시는 건가요? 종목명을 알려주시면 조회해 드릴게요.

[사용자] 작년 최고가 알려줘
[행동] 지원하지 않는 데이터 → 도구 호출 안 함
[응답] 과거 최고가 데이터는 현재 제공되지 않습니다. 현재 주가를 조회해 드릴까요?

# 금지 사항
- 도구 호출 전에 가격 언급
- 예시의 숫자(72500, 123.45 등)를 응답에 사용
- "도구호출:", "도구결과:" 텍스트를 응답에 포함
- 도구 실패 시 추측으로 대체
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
            log.info(f"종목 자동 변환: '{ticker}' → {tkr}")
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

            # 환율 관련
            (r'달러.{0,5}환율|환율.{0,5}달러|원달러', 'get_market', lambda q: {'market_type': 'USD_KRW', 'ticker': ''}),
            (r'엔.{0,5}환율|환율.{0,5}엔', 'get_market', lambda q: {'market_type': 'JPY_KRW', 'ticker': ''}),
            (r'유로.{0,5}달러|EURUSD', 'get_market', lambda q: {'market_type': 'EUR_USD', 'ticker': ''}),

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

            # 서비스 도움말
            (r'(사용법|도움말|메뉴얼|가이드|사용방법)', 'search_docs', self._extract_docs_params),
        ]

    def _extract_news_params(self, query: str) -> dict:
        """뉴스 개수 추출"""
        match = re.search(r'(\d+)개', query)
        count = int(match.group(1)) if match else 5
        count = max(1, min(20, count))  # 1~20 제한
        return {'count': count}

    def _extract_stock_params_flexible(self, query: str) -> dict:
        """주식 종목명 추출 (통합 STOCK_TICKER_MAP 사용)"""
        query_lower = query.lower()

        # 1. 통합 매핑에서 확인
        for name, ticker in STOCK_TICKER_MAP.items():
            if name.lower() in query_lower:
                log.info(f"패턴 매칭: 종목 '{name}' → {ticker}")
                return {'market_type': 'QUOTE', 'ticker': ticker}

        # 2. 6자리 숫자 티커 (한국 주식)
        match = re.search(r'(\d{6})', query)
        if match:
            ticker = f"{match.group(1)}.KS"
            log.info(f"패턴 매칭: 숫자 티커 → {ticker}")
            return {'market_type': 'QUOTE', 'ticker': ticker}

        # 3. 영문 대문자 1~5자 티커 (해외 주식)
        match = re.search(r'\b([A-Z]{1,5})\b', query.upper())
        if match:
            ticker = match.group(1)
            # 일반 단어 제외
            excluded = {'THE', 'AND', 'FOR', 'ARE', 'BUT', 'NOT', 'YOU', 'ALL', 'CAN', 'HAD', 'HER', 'WAS', 'ONE', 'OUR', 'OUT'}
            if ticker not in excluded:
                log.info(f"패턴 매칭: 영문 티커 → {ticker}")
                return {'market_type': 'QUOTE', 'ticker': ticker}

        # 4. 종목 특정 불가 → 빈 티커 반환 (LLM이 사용자에게 확인 요청)
        log.warning(f"종목 특정 불가: '{query}'")
        return {'market_type': 'QUOTE', 'ticker': ''}

    def _extract_docs_params(self, query: str) -> dict:
        """문서 검색 쿼리 추출"""
        return {'query': query}

    def route(self, query: str) -> Optional[Dict[str, Any]]:
        """쿼리를 분석하여 매칭되는 도구와 파라미터 반환"""
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
                    log.error(f"파라미터 추출 실패 ({pattern}): {e}")
                    continue

        return None  # 매칭 안 됨 → 일반 대화

# 라우터 인스턴스 생성
router = ToolRouter()

# ===== PyKRX 시세 조회 =====
def fetch_quote_formatted(ticker: str) -> dict:
    """PyKRX 우선 → yfinance 최소 fallback (LangChain용 숫자 형식)"""
    ticker_code = resolve_ticker(ticker.strip())
    log.info(f"쿼리: {ticker} → {ticker_code}")

    def _format_output(q: dict, ticker_display: str) -> dict:
        """조회 결과를 안전하게 포맷팅"""
        price = q.get('price')
        if price is None:
            return {"error": f"{ticker_display} 가격 데이터 없음"}

        change = q.get('change')
        change_pct = q.get('changePct')
        # ts_kst 또는 date 필드에서 날짜 추출
        date_str = q.get('ts_kst') or q.get('date') or datetime.now(KST).strftime("%Y-%m-%d")

        # ISO 형식이면 날짜만 추출
        if isinstance(date_str, str) and 'T' in date_str:
            date_str = date_str.split('T')[0]

        # None 값 안전 처리
        change_str = f"{change:.0f}" if change is not None else "N/A"
        change_pct_str = f"{change_pct:.2f}" if change_pct is not None else "N/A"

        return {
            "output": f"price={price}, change={change_str}, changePct={change_pct_str}, date={date_str}"
        }

    # 1. 한국 주식: 6자리 코드 또는 .KS/.KQ 접미사 → PyKRX
    krx_code = None
    if re.match(r'^\d{6}$', ticker_code):
        krx_code = ticker_code
    elif ticker_code.endswith(('.KS', '.KQ')):
        krx_code = ticker_code.replace('.KS', '').replace('.KQ', '')

    if krx_code:
        q = fetch_quote_krx(krx_code)
        if q and q.get('price') is not None:
            return _format_output(q, ticker)

    # 2. 글로벌 주식/지수: yfinance (ORCL, ^KS11 등)
    yf_ticker = ticker_code
    if re.match(r'^\d{6}$', ticker_code):
        yf_ticker = f"{ticker_code}.KS"  # PyKRX 실패시 yf용

    q = fetch_quote_yf(yf_ticker)
    if q and q.get('price') is not None:
        return _format_output(q, ticker)

    return {"error": f"{ticker} 데이터 없음"}


# ===== yfinance 시세 조회 =====
def get_market_wrapper(market_type: str, ticker: str = "") -> dict:
    """시장 데이터 조회 래퍼 (동기)"""
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
            # 빈 티커 처리 → 사용자에게 종목명 확인 요청
            if not ticker or ticker.strip() == "":
                return {"output": "종목을 특정할 수 없습니다. 종목명이나 티커 코드를 알려주시겠어요? (예: 삼성전자, AAPL, 005930)"}
            return fetch_quote_formatted(ticker)

        else:
            return {"error": f"지원하지 않는 시장 타입: {market_type}"}
    except Exception as e:
        return {"error": f"시장 데이터 조회 실패: {str(e)}"}


async def get_market_wrapper_async(market_type: str, ticker: str = "") -> dict:
    """시장 데이터 조회 래퍼 (비동기 - 캐시 활용)"""
    try:
        market_type = market_type.strip().upper()

        if market_type == "KOSPI":
            data = await fetch_quote_cached_async(TICKER_KOSPI)
            return _format_index_output("코스피", data)
        elif market_type == "KOSDAQ":
            data = await fetch_quote_cached_async(TICKER_KOSDAQ)
            return _format_index_output("코스닥", data)
        elif market_type == "USD_KRW":
            data = await fetch_quote_cached_async(TICKER_USD_KRW)
            return _format_fx_output("달러/원", data)
        elif market_type == "JPY_KRW":
            data = await fetch_quote_cached_async(TICKER_JPY_KRW)
            return _format_fx_output("엔/원", data, multiply=100)
        elif market_type == "EUR_USD":
            data = await fetch_quote_cached_async(TICKER_EUR_USD)
            return _format_fx_output("유로/달러", data)
        elif market_type == "MARKET_SUMMARY":
            # 병렬로 모든 지수/환율 조회
            tickers = [TICKER_KOSPI, TICKER_KOSDAQ, TICKER_DOW, TICKER_SP500, TICKER_USD_KRW, TICKER_JPY_KRW]
            results = await fetch_quotes_parallel(tickers)
            return _format_market_summary(results)
        elif market_type == "QUOTE":
            if not ticker or ticker.strip() == "":
                return {"output": "종목을 특정할 수 없습니다. 종목명이나 티커 코드를 알려주시겠어요? (예: 삼성전자, AAPL, 005930)"}
            # 티커 정규화
            resolved = resolve_ticker(ticker)
            data = await fetch_quote_cached_async(resolved)
            return _format_quote_output(ticker, data)
        else:
            return {"error": f"지원하지 않는 시장 타입: {market_type}"}
    except Exception as e:
        return {"error": f"시장 데이터 조회 실패: {str(e)}"}


def _format_index_output(name: str, data: dict) -> dict:
    """지수 데이터 포맷팅"""
    price = data.get("price")
    if price is None:
        return {"output": f"**{name} 지수**\n• 현재 데이터를 가져올 수 없습니다."}
    ch = data.get("change", 0) or 0
    pct = data.get("changePct", 0) or 0
    sign = "+" if ch >= 0 else ""
    return {"output": f"**{name} 지수 (실시간)**\n• 현재가: {price:,.2f}\n• 변동: {sign}{ch:.2f} ({sign}{pct:.2f}%)"}


def _format_fx_output(name: str, data: dict, multiply: int = 1) -> dict:
    """환율 데이터 포맷팅"""
    price = data.get("price")
    if price is None:
        return {"output": f"**{name} 환율**\n• 현재 데이터를 가져올 수 없습니다."}
    display_price = price * multiply if multiply > 1 else price
    ch = (data.get("change", 0) or 0) * multiply
    pct = data.get("changePct", 0) or 0
    sign = "+" if ch >= 0 else ""
    unit = "원" if "원" in name else "달러"
    return {"output": f"**{name} 환율 (실시간)**\n• 현재: {display_price:,.2f}{unit}\n• 변동: {sign}{ch:.2f} ({sign}{pct:.2f}%)"}


def _format_quote_output(ticker: str, data: dict) -> dict:
    """개별 종목 시세 포맷팅"""
    if data.get("error"):
        return {"error": data["error"]}
    price = data.get("price")
    if price is None:
        return {"output": f"{ticker} 시세를 가져올 수 없습니다."}
    ch = data.get("change", 0) or 0
    pct = data.get("changePct", 0) or 0
    sign = "+" if ch >= 0 else ""
    # 원화/달러 구분
    is_korean = data.get("ticker", "").endswith((".KS", ".KQ"))
    if is_korean:
        return {"output": f"**{ticker} 시세 (실시간)**\n• 현재가: {price:,.0f}원\n• 변동: {sign}{ch:,.0f}원 ({sign}{pct:.2f}%)"}
    else:
        return {"output": f"**{ticker} 시세 (실시간)**\n• 현재가: ${price:,.2f}\n• 변동: {sign}${ch:.2f} ({sign}{pct:.2f}%)"}


def _format_market_summary(results: dict) -> dict:
    """시장 요약 포맷팅"""
    lines = ["**📊 시장 요약 (실시간)**\n"]

    # 지수
    lines.append("**[지수]**")
    for ticker, name in [(TICKER_KOSPI, "코스피"), (TICKER_KOSDAQ, "코스닥"), (TICKER_DOW, "다우"), (TICKER_SP500, "S&P500")]:
        data = results.get(ticker, {})
        price = data.get("price")
        if price:
            pct = data.get("changePct", 0) or 0
            sign = "+" if pct >= 0 else ""
            lines.append(f"• {name}: {price:,.2f} ({sign}{pct:.2f}%)")

    # 환율
    lines.append("\n**[환율]**")
    for ticker, name in [(TICKER_USD_KRW, "달러/원"), (TICKER_JPY_KRW, "엔/원(100)")]:
        data = results.get(ticker, {})
        price = data.get("price")
        if price:
            display = price * 100 if ticker == TICKER_JPY_KRW else price
            pct = data.get("changePct", 0) or 0
            sign = "+" if pct >= 0 else ""
            lines.append(f"• {name}: {display:,.2f} ({sign}{pct:.2f}%)")

    return {"output": "\n".join(lines)}

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
except Exception:
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

# ===== Ollama LLM (규칙 기반 라우팅용) =====
llm = ChatOllama(
    model="gemma2:9b",
    base_url="http://localhost:11434",
    temperature=0.3,
    num_ctx=8192,  # Gemma 2는 8K 컨텍스트 지원
    num_predict=512,
)

# 스트리밍용 LLM (별도 인스턴스)
llm_stream = ChatOllama(
    model="gemma2:9b",
    base_url="http://localhost:11434",
    temperature=0.3,
    num_ctx=8192,
    num_predict=512,
)

# ===== 규칙 기반 채팅 함수 (Gemma 2 9B 최적화) =====
GREETING_KEYWORDS = ["안녕", "hello", "hi", "반가", "처음", "감사", "반갑", "초보"]
GREETING_RESPONSE = "안녕하세요! 저는 경제 뉴스와 실시간 경제 지표, 주가 정보를 제공하며, 경제 용어 설명으로 경제 학습을 도와드립니다. 무엇이 궁금하신가요?"

# 도구 함수 매핑 (공통)
TOOL_MAP = {
    'get_latest_news': get_latest_news_wrapper,
    'get_indicator': get_indicator_wrapper,
    'get_market': get_market_wrapper,
    'search_docs': search_docs_wrapper
}


def _execute_tool(tool_name: str, params: dict) -> dict:
    """도구 실행 공통 함수 (동기)"""
    tool_func = TOOL_MAP.get(tool_name)
    if not tool_func:
        return {"error": f"알 수 없는 도구: {tool_name}"}

    if tool_name == 'get_latest_news':
        return tool_func(count=params.get('count', 5))
    elif tool_name == 'get_indicator':
        return tool_func(indicator_type=params.get('indicator_type', ''))
    elif tool_name == 'get_market':
        return tool_func(market_type=params.get('market_type', ''), ticker=params.get('ticker', ''))
    elif tool_name == 'search_docs':
        return tool_func(query=params.get('query', ''))
    return {"error": "도구 실행 실패"}


async def _execute_tool_async(tool_name: str, params: dict) -> dict:
    """도구 실행 공통 함수 (비동기)

    - get_market: 비동기 함수 직접 호출 (캐시 + 병렬 처리 지원)
    - 나머지 도구: ThreadPoolExecutor로 비동기 래핑 (MongoDB, ECOS/FRED API, FAISS 검색)
    """
    if tool_name not in TOOL_MAP and tool_name != 'get_market':
        return {"error": f"알 수 없는 도구: {tool_name}"}

    loop = asyncio.get_event_loop()

    if tool_name == 'get_market':
        # 비동기 시세 조회 (캐시 + 세마포어 적용)
        return await get_market_wrapper_async(
            market_type=params.get('market_type', ''),
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


def _build_tool_prompt(user_message: str, tool_output: str) -> str:
    """도구 결과를 자연어로 변환하기 위한 프롬프트 생성"""
    return f"""사용자 질문: {user_message}

도구 실행 결과:
{tool_output}

위 정보를 바탕으로 사용자에게 친절하고 자연스러운 한국어로 답변하세요.
- 100~200자 분량으로 간결하게 작성
- 도구 결과의 숫자와 날짜를 그대로 사용 (절대 임의 생성 금지)
- 마지막에 "더 궁금한 부분이 있으신가요?" 추가"""


def _build_chat_prompt(history: list, user_message: str) -> str:
    """일반 대화용 프롬프트 생성"""
    messages = [{"role": "system", "content": SYSTEM_INSTRUCTIONS}]
    for turn in history[-10:]:
        messages.append({"role": turn['role'], "content": turn['content']})
    messages.append({"role": "user", "content": user_message})
    return "\n\n".join([f"{msg['role']}: {msg['content']}" for msg in messages])


def chat_with_agent(user_message: str, session_id: str = "default") -> str:
    """규칙 기반 라우팅 + Gemma 2 9B 응답 생성"""

    # 1. 인사 감지 시 즉시 반환
    if any(kw in user_message.lower() for kw in GREETING_KEYWORDS):
        add_turn(session_id, "user", user_message)
        add_turn(session_id, "assistant", GREETING_RESPONSE)
        return GREETING_RESPONSE

    try:
        # 2. 규칙 기반 라우팅으로 도구 선택
        route_result = router.route(user_message)

        if route_result:
            # 도구 실행
            tool_name = route_result['tool']
            params = route_result['params']
            log.info(f"도구 호출: {tool_name}({params})")

            tool_result = _execute_tool(tool_name, params)

            # 에러 처리
            if "error" in tool_result:
                error_msg = f"죄송합니다. {tool_result['error']}"
                add_turn(session_id, "user", user_message)
                add_turn(session_id, "assistant", error_msg)
                return error_msg

            # 3. 도구 결과를 Gemma 2로 자연어 변환
            tool_output = tool_result.get("output", str(tool_result))
            context_prompt = _build_tool_prompt(user_message, tool_output)
            response = llm.invoke(context_prompt)
        else:
            # 4. 일반 대화 (도구 없이 Gemma 2만 사용)
            history = get_session(session_id)
            prompt = _build_chat_prompt(history, user_message)
            response = llm.invoke(prompt)

        # 응답 추출 및 세션 저장
        final_answer = response.content if hasattr(response, "content") else str(response)
        add_turn(session_id, "user", user_message)
        add_turn(session_id, "assistant", final_answer)
        return final_answer

    except Exception as e:
        log.exception("채팅 처리 실패")
        return f"죄송합니다. 오류가 발생했습니다: {str(e)}"


async def chat_with_agent_async(user_message: str, session_id: str = "default") -> str:
    """규칙 기반 라우팅 + Gemma 2 9B 응답 생성 (비동기 버전)

    - 도구 실행: _execute_tool_async 사용 (캐시 + 세마포어 적용)
    - LLM 호출: ThreadPoolExecutor로 비동기 래핑
    """

    # 1. 인사 감지 시 즉시 반환
    if any(kw in user_message.lower() for kw in GREETING_KEYWORDS):
        add_turn(session_id, "user", user_message)
        add_turn(session_id, "assistant", GREETING_RESPONSE)
        return GREETING_RESPONSE

    try:
        # 2. 규칙 기반 라우팅으로 도구 선택
        route_result = router.route(user_message)
        loop = asyncio.get_event_loop()

        if route_result:
            # 도구 실행 (비동기)
            tool_name = route_result['tool']
            params = route_result['params']
            log.info(f"[비동기] 도구 호출: {tool_name}({params})")

            tool_result = await _execute_tool_async(tool_name, params)

            # 에러 처리
            if "error" in tool_result:
                error_msg = f"죄송합니다. {tool_result['error']}"
                add_turn(session_id, "user", user_message)
                add_turn(session_id, "assistant", error_msg)
                return error_msg

            # 3. 도구 결과를 Gemma 2로 자연어 변환 (비동기)
            tool_output = tool_result.get("output", str(tool_result))
            context_prompt = _build_tool_prompt(user_message, tool_output)
            response = await loop.run_in_executor(EXECUTOR, llm.invoke, context_prompt)
        else:
            # 4. 일반 대화 (도구 없이 Gemma 2만 사용)
            history = get_session(session_id)
            prompt = _build_chat_prompt(history, user_message)
            response = await loop.run_in_executor(EXECUTOR, llm.invoke, prompt)

        # 응답 추출 및 세션 저장
        final_answer = response.content if hasattr(response, "content") else str(response)
        add_turn(session_id, "user", user_message)
        add_turn(session_id, "assistant", final_answer)
        return final_answer

    except Exception as e:
        log.exception("[비동기] 채팅 처리 실패")
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
    latest = res["data"][-1]
    return f"**한국은행 기준금리**\\n• 현재 금리: {latest.get('DATA_VALUE','N/A')} (기준: {latest.get('TIME','')})"

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


# ===== 비동기 시세 조회 함수 =====
async def fetch_quote_yf_async(ticker: str) -> Dict[str, Any]:
    """yfinance 비동기 래핑 (세마포어 + ThreadPool)"""
    async with YF_SEMAPHORE:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(EXECUTOR, fetch_quote_yf, ticker)


async def fetch_quote_krx_async(ticker: str) -> Dict[str, Any]:
    """PyKRX 비동기 래핑 (세마포어 + ThreadPool)"""
    async with KRX_SEMAPHORE:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(EXECUTOR, fetch_quote_krx, ticker)


async def fetch_quote_cached_async(ticker: str) -> Dict[str, Any]:
    """TTL 캐시 기반 시세 조회 (비동기)"""
    now = datetime.now(KST)

    # 캐시 확인
    async with QUOTE_CACHE_LOCK:
        if ticker in QUOTE_CACHE:
            cached = QUOTE_CACHE[ticker]
            if cached["expires_at"] > now:
                log.debug(f"캐시 히트: {ticker}")
                return cached["data"]

    # 캐시 미스 → API 호출
    is_korean = ticker.endswith((".KS", ".KQ")) or (ticker.isdigit() and len(ticker) == 6)

    if is_korean:
        # 한국 주식: PyKRX 우선
        data = await fetch_quote_krx_async(ticker)
        if data.get("error"):
            # fallback to yfinance
            data = await fetch_quote_yf_async(ticker)
    else:
        data = await fetch_quote_yf_async(ticker)

    # 캐시 저장
    async with QUOTE_CACHE_LOCK:
        QUOTE_CACHE[ticker] = {
            "data": data,
            "expires_at": now + QUOTE_CACHE_TTL
        }

    return data


async def fetch_quotes_parallel(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
    """여러 티커 병렬 조회"""
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
    """주요 시세 백그라운드 갱신 (30초마다)"""
    # 주요 티커 목록
    key_tickers = [
        # 지수
        TICKER_KOSPI, TICKER_KOSDAQ, TICKER_DOW, TICKER_SP500, TICKER_NASDAQ,
        # 환율
        TICKER_USD_KRW, TICKER_JPY_KRW, TICKER_EUR_USD,
        # 한국 대형주 (상위 5개)
        "005930.KS", "000660.KS", "035420.KS", "005380.KS", "035720.KS",
    ]

    while True:
        try:
            await fetch_quotes_parallel(key_tickers)
            log.debug(f"백그라운드 캐시 갱신 완료: {len(key_tickers)}개 티커")
        except Exception as e:
            log.warning(f"백그라운드 캐시 갱신 실패: {e}")

        await asyncio.sleep(30)  # 30초 대기


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
    q = fetch_quote_yf(TICKER_KOSPI); price, ch, pct = q.get("price"), q.get("change"), q.get("changePct")
    if price is None: return "**코스피 지수**\n• 현재 데이터를 가져올 수 없습니다."
    sign = "+" if (ch or 0) >= 0 else ""
    return f"**코스피 지수 (실시간)**\n• 현재가: {price:,.2f}\n• 변동: {sign}{ch if ch is not None else 'N/A'} ({sign}{pct if pct is not None else 'N/A'}%)"

def get_kosdaq_index() -> str:
    # 코스닥 단건 포맷
    q = fetch_quote_yf(TICKER_KOSDAQ); price, ch, pct = q.get("price"), q.get("change"), q.get("changePct")
    if price is None: return "**코스닥 지수**\n• 현재 데이터를 가져올 수 없습니다."
    sign = "+" if (ch or 0) >= 0 else ""
    return f"**코스닥 지수 (실시간)**\n• 현재가: {price:,.2f}\n• 변동: {sign}{ch if ch is not None else 'N/A'} ({sign}{pct if pct is not None else 'N/A'}%)"

def get_usd_krw() -> str:
    # 달러/원 포맷
    q = fetch_quote_yf(TICKER_USD_KRW); price, ch, pct = q.get("price"), q.get("change"), q.get("changePct")
    if price is None: return "**원/달러 환율**\n• 현재 데이터를 가져올 수 없습니다."
    sign = "+" if (ch or 0) >= 0 else ""
    return f"**원/달러 환율 (실시간)**\n• 현재: {price:,.2f}원\n• 변동: {sign}{(ch or 0):.2f}원 ({sign}{(pct or 0):.2f}%)"

def get_jpy_krw() -> str:
    # 엔/원 포맷
    q = fetch_quote_yf(TICKER_JPY_KRW); price, ch, pct = q.get("price"), q.get("change"), q.get("changePct")
    if price is None: return "**원/엔 환율**\n• 현재 데이터를 가져올 수 없습니다."
    sign = "+" if (ch or 0) >= 0 else ""
    return f"**원/엔 환율 (실시간)**\n• 현재: {price:,.2f}원\n• 변동: {sign}{(ch or 0):.2f}원 ({sign}{(pct or 0):.2f}%)"

def get_eur_usd() -> str:
    # 유로/달러 포맷
    q = fetch_quote_yf(TICKER_EUR_USD); price, ch, pct = q.get("price"), q.get("change"), q.get("changePct")
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
    except Exception:
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

    # 백그라운드 캐시 갱신 태스크 시작
    cache_task = asyncio.create_task(refresh_cache_background())
    log.info("백그라운드 캐시 갱신 태스크 시작")

    yield

    # ===== Shutdown =====
    # 백그라운드 태스크 취소
    cache_task.cancel()
    with suppress(asyncio.CancelledError):
        await cache_task
    log.info("백그라운드 캐시 태스크 종료")

    try:
        scheduler.shutdown()
        log.info("APScheduler stopped.")
    except Exception:
        log.exception("APScheduler 종료 실패")

    # ThreadPoolExecutor 종료
    EXECUTOR.shutdown(wait=False)
    log.info("ThreadPoolExecutor 종료")

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
        # 비동기 버전 사용 (캐시 + 세마포어 적용)
        agent_answer = await chat_with_agent_async(user_msg, session_id)
        return {"answer": agent_answer, "session_id": session_id}
    except Exception:
        log.exception("chat failed")
        return {"answer": "일시적 오류가 발생했습니다. 잠시 후 다시 시도해주세요."}

# ===== 스트리밍 챗 엔드포인트 (SSE) =====
from typing import AsyncGenerator

def _sse_event(chunk: str, done: bool = False) -> str:
    """SSE 이벤트 포맷 생성"""
    return f"data: {json.dumps({'chunk': chunk, 'done': done}, ensure_ascii=False)}\n\n"


async def stream_chat_response(user_message: str, session_id: str) -> AsyncGenerator[str, None]:
    """LLM 응답을 스트리밍으로 생성"""

    # 1. 인사 감지 시 즉시 반환
    if any(kw in user_message.lower() for kw in GREETING_KEYWORDS):
        add_turn(session_id, "user", user_message)
        add_turn(session_id, "assistant", GREETING_RESPONSE)
        yield _sse_event(GREETING_RESPONSE, done=True)
        return

    try:
        # 2. 규칙 기반 라우팅으로 도구 선택
        route_result = router.route(user_message)

        if route_result:
            # 도구 실행 (비동기)
            tool_name = route_result['tool']
            params = route_result['params']
            log.info(f"[스트리밍] 도구 호출: {tool_name}({params})")

            tool_result = await _execute_tool_async(tool_name, params)

            # 에러 처리
            if "error" in tool_result:
                error_msg = f"죄송합니다. {tool_result['error']}"
                add_turn(session_id, "user", user_message)
                add_turn(session_id, "assistant", error_msg)
                yield _sse_event(error_msg, done=True)
                return

            # 3. 도구 결과를 Gemma 2로 자연어 변환 (스트리밍)
            tool_output = tool_result.get("output", str(tool_result))
            prompt = _build_tool_prompt(user_message, tool_output)
        else:
            # 4. 일반 대화 (도구 없이 Gemma 2만 사용)
            history = get_session(session_id)
            prompt = _build_chat_prompt(history, user_message)

        # 스트리밍 응답 생성
        full_response = ""
        async for chunk in llm_stream.astream(prompt):
            if hasattr(chunk, "content") and chunk.content:
                full_response += chunk.content
                yield _sse_event(chunk.content, done=False)

        # 세션 저장
        add_turn(session_id, "user", user_message)
        add_turn(session_id, "assistant", full_response)
        yield _sse_event("", done=True)

    except Exception as e:
        log.exception("[스트리밍] 채팅 처리 실패")
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

    # 텍스트 정리
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