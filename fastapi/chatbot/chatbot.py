# chatbot.py — GPT-5 + RAG + Open API + MongoDB + Function Calling

# 1) Chatbot 파트: OpenAI(Function Calling), MongoDB 최신뉴스, ECOS/FRED 경제지표, yfinance 경제시세, RAG 파일검색
# 2) STT 파트: CLOVA STT + ffmpeg 전처리
# 3) TTS 파트: Google Cloud Text-to-Speech

# ===== 기본 임포트 =====
# 표준/서드파티 라이브러리 로드 (FastAPI, OpenAI, MongoDB, APScheduler, GCP TTS, yfinance, pandas 등)
import os, logging, subprocess, io, requests, tempfile, re, shutil, json
from typing import Dict, Any, List, Optional
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, UploadFile, File, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from openai import OpenAI
from pymongo import MongoClient, DESCENDING
from apscheduler.schedulers.background import BackgroundScheduler
from google.cloud import texttospeech
from google.oauth2 import service_account

import yfinance as yf
import pandas as pd

# ===== 로깅 =====
# 전역 로거 설정 (레벨/포맷)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("chatbot")

# ===== 고정 상수 =====
# KST 타임존 상수
KST = ZoneInfo("Asia/Seoul")

# ===== OpenAI =====
# OPENAI_API_KEY 환경변수 사용, 고정 UA 부여
API_KEY = os.getenv("OPENAI_API_KEY", "")
client = OpenAI(api_key=API_KEY, default_headers={"User-Agent": "dgict-bot/1.0"})

# =============================================================
# CHATBOT (RAG + 뉴스 + 지표 + 시세 + Function Calling + 세션/라우트)
# =============================================================

# ===== 시스템 프롬프트 =====
# 답변 톤/형식, 도구 사용 원칙 요약
SYSTEM_INSTRUCTIONS = """
너는 'AI 기반 경제 뉴스 분석 웹서비스'의 안내 챗봇이다. 사용자는 '바로 결과'를 원한다.
- 결론부터 3~6문장 또는 불릿으로 간결히 답하라.
- 가능하면 '제목(링크) · 발행일(KST) · 한줄 요약' 구조를 쓴다.
- 어려운 용어는 괄호로 짧게 보충한다. (예: 리프라이싱=재가격조정)
- 에러/빈결과는 한 줄로 원인 + 1가지 대안만 제시한다.
- TTS기능을 사용해야 하기 때문에 사용자에게 말하듯 답하라(이모티콘 사용 금지) 

도구 사용 정책:
- 최신 뉴스/핫이슈: get_latest_news
- 경제지표(CPI, PPI, GDP, 기준금리/무역수지/경상수지, 미국 금리): get_indicator
- 주가지수/환율: get_market
- 웹서비스 기능/사용법/도움말: search_docs
- 그 외 일반 질문은 도구 없이 답하라. (GPT-5모델)
"""

# ===== Function Calling 스키마 =====
# 모델이 호출할 수 있는 함수 정의 (뉴스/지표/시세/RAG)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_latest_news",
            "description": "MongoDB에서 최신 경제 뉴스 N건을 조회한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5}
                },
                "required": ["count"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_indicator",
            "description": "경제지표 조회 (ECOS: CPI/PPI/GDP/기준금리/무역수지/경상수지, FRED: 미국 금리).",
            "parameters": {
                "type": "object",
                "properties": {
                    "indicator_type": {
                        "type": "string",
                        "enum": [
                            "CPI", "PPI", "GDP", "BASE_RATE", "TRADE_BALANCE", "CURRENT_ACCOUNT",
                            "US_FEDFUNDS", "US_FED_TARGET"
                        ]
                    }
                },
                "required": ["indicator_type"]
            }
        }
    },
    {
      "type": "function",
      "function": {
        "name": "get_market",
        "description": "주요 지수/환율 및 개별 종목 시세를 조회한다.",
        "parameters": {
          "type": "object",
          "properties": {
            "market_type": {
              "type": "string",
              "enum": [
                "KOSPI",
                "KOSDAQ",
                "MARKET_SUMMARY",
                "USD_KRW",
                "JPY_KRW",
                "EUR_USD",
                "QUOTE"
              ]
            },
            "ticker": {
              "type": "string",
              "description": "개별 종목 심볼 (예: NVDA, AAPL, 005930.KS, 086520.KQ)"
            }
          },
          "required": ["market_type"]
        }
      }
    },

    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "웹서비스 기능/도움말은 파일검색(RAG)로 문서를 바탕으로 답한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"}
                },
                "required": ["query"]
            }
        }
    }
]

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
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://Dgict_TeamB:team1234@cluster0.5d0uual.mongodb.net/")
DB_NAME = "test123"
COLL_NAME = "chatbot_rag"

def _get_db():
    # DB 핸들 반환
    return MongoClient(MONGO_URI)[DB_NAME]

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
    # 뉴스 목록을 간단한 MD 텍스트로 변환
    if not rows: return "최신 경제 뉴스가 없습니다."
    out = ["**최신 경제 뉴스**"]
    for i, r in enumerate(rows, 1):
        title = (r.get("title") or "").strip() or "(제목 없음)"
        url = (r.get("url") or "").strip()
        date = r.get("published_at", "")
        if url:
            out.append(f"{i}. [{title}]\n출처: ({url}) · 날짜: {date}")
        else:
            out.append(f"{i}. {title} · {date}")
    return "\n".join(out)

# ===== FRED =====
# API 키/엔드포인트 상수
FRED_KEY = os.getenv("FRED_API_KEY", "")
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# ===== FRED 조회 유틸 =====
# 관측치 조회(빈값 필터), FEDFUNDS/목표범위 처리
def _fred_observations(series_id: str, start: str = "2024-01-01") -> list:
    params = {
        "series_id": series_id,
        "api_key": FRED_KEY,
        "file_type": "json",
        "observation_start": start
    }
    r = requests.get(FRED_BASE, params=params, timeout=20)
    r.raise_for_status()
    obs = r.json().get("observations", []) or []
    return [o for o in obs if o.get("value") not in ("", ".")]

def get_us_fed_funds_latest(use_target_range: bool = False) -> dict:
    # FEDFUNDS(월) 또는 DFEDTARU/L(일) 최신값 반환
    try:
        if use_target_range:
            up = _fred_observations("DFEDTARU")
            lo = _fred_observations("DFEDTARL")
            if not up or not lo:
                raise RuntimeError("target range observations empty")
            up_last, lo_last = up[-1], lo[-1]
            date = up_last["date"]
            upper = float(up_last["value"])
            lower = float(lo_last["value"])
            return {"date": date, "value": upper, "lower": lower, "upper": upper, "unit": "%", "source": "FRED"}
        else:
            obs = _fred_observations("FEDFUNDS")
            if not obs:
                raise RuntimeError("fedfunds observations empty")
            last = obs[-1]
            return {"date": last["date"], "value": float(last["value"]), "unit": "%", "source": "FRED"}
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
    res = fetch_all_key_statistics()
    if "error" in res: return f"기준금리 조회 실패: {res['error']}"
    for ind in res["indicators"]:
        nm = (ind.get("KEYSTAT_NAME") or "").upper()
        if "기준" in nm or "BASE RATE" in nm:
            return f"**한국은행 기준금리**\n• 현재 금리: {ind.get('DATA_VALUE','N/A')}{ind.get('UNIT_NAME','%')} (기준: {ind.get('TIME','')})"
    return "기준금리 정보를 찾을 수 없습니다."

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
    "HYUNDAI_MOTOR":       {"ticker": "005380.KS", "name": "현대차"},
    "KIA":                 {"ticker": "000270.KS", "name": "기아"},
    "NAVER":               {"ticker": "035420.KS", "name": "NAVER"},
    "KAKAO":               {"ticker": "035720.KS", "name": "카카오"},
    "POSCO_HOLDINGS":      {"ticker": "005490.KS", "name": "POSCO홀딩스"},
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

def get_market_indices() -> str:
    # 주요 지수 요약 문자열 생성
    results = []
    for key, info in INDEX_MAP.items():
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

# ===== 도구 실행기 =====
# Function Call 이름 → 실제 함수 라우팅/출력 포맷
def run_tool(tool_name: str, arguments: dict) -> dict:
    try:
        if tool_name == "get_latest_news":
            n = int(arguments.get("count", 5))
            rows = fetch_latest_topn_from_mongo(n)
            return {"ok": True, "markdown": format_topn_md(rows)}

        elif tool_name == "get_indicator":
            t = (arguments.get("indicator_type") or "").upper()
            if t == "CPI": data = get_cpi_data()
            elif t == "PPI": data = get_ppi_data()
            elif t == "GDP": data = get_gdp_data()
            elif t == "BASE_RATE": data = get_base_rate()
            elif t == "TRADE_BALANCE": data = get_trade_balance()
            elif t == "CURRENT_ACCOUNT": data = get_current_account()
            elif t == "US_FEDFUNDS":
                d = get_us_fed_funds_latest(False)
                if "error" in d:
                    data = "미국 실효 연방기금금리 조회에 실패했습니다. 잠시 후 다시 시도해 주세요."
                else:
                    data = f"**미국 실효 연방기금금리(FEDFUNDS)**\n• 최신값: {d['value']:.2f}{d.get('unit','%')} (기준: {d['date']})"
            elif t == "US_FED_TARGET":
                d = get_us_fed_funds_latest(True)
                if "error" in d:
                    data = "미국 연방기금금리 목표범위 조회에 실패했습니다."
                else:
                    rng = f"{d['lower']:.2f}–{d['upper']:.2f}{d.get('unit','%')}"
                    data = f"**미국 연방기금금리 목표범위**\n• 범위: {rng} (기준: {d['date']})"
            else:
                data = "지원하지 않는 지표입니다."
            return {"ok": True, "markdown": data}

        elif tool_name == "get_market":
            t = (arguments.get("market_type") or "").upper()
            if t == "KOSPI":
                data = get_kospi_index()
            elif t == "KOSDAQ":
                data = get_kosdaq_index()
            elif t == "USD_KRW":
                data = get_usd_krw()
            elif t == "JPY_KRW":
                data = get_jpy_krw()
            elif t == "EUR_USD":
                data = get_eur_usd()
            elif t == "MARKET_SUMMARY":
                data = f"{get_market_indices()}\n\n{get_fx_rates()}"
            elif t == "QUOTE":
                ticker = (arguments.get("ticker") or "").strip()
                q = fetch_quote_yf(ticker)
                if q.get("price") is not None:
                    ch, pct = q.get("change"), q.get("changePct")
                    sign = "+" if (ch or 0) >= 0 else ""
                    # f-string은 % 기호 이스케이프 불필요하므로 안전함
                    data = (
                        f"{ticker.upper()} {q['price']:,.2f} · 변동 {sign}{(ch or 0):.2f} "
                        f"({sign}{(pct or 0):.2f}%) · 기준시각 {q.get('ts_kst', '')}"
                    )
                else:
                    data = (
                        f"시세 API 응답이 비정상이라 {ticker.upper()} 현재가를 가져오지 못했습니다; "
                        f"대안: 야후파이낸스에서 티커 {ticker.upper()}로 실시간 가격을 확인해 주세요."
                    )
            else:
                data = "지원하지 않는 시장 데이터입니다."
            return {"ok": True, "markdown": data}

        elif tool_name == "search_docs":
            q = arguments.get("query") or ""
            resp = client.responses.create(
                model="gpt-5",
                instructions=SYSTEM_INSTRUCTIONS,
                tools=[{"type": "file_search", "vector_store_ids": [VS_ID]}],
                input=[{"role":"user","content":[{"type":"input_text","text":q}]}],
            )
            ans = (getattr(resp, "output_text", "") or "").strip() or "문서에서 답을 찾지 못했습니다."
            return {"ok": True, "markdown": ans}

        return {"ok": False, "error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        log.exception("Tool execution failed")
        return {"ok": False, "error": str(e)}

# ===== FastAPI 앱/CORS =====
# 앱 인스턴스 생성, 전역 CORS 허용(데모 편의)
app = FastAPI(title="Chat+RAG+News+Indicators (Function Calling)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)
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

# ===== 메인 챗 엔드포인트 =====
# 사용자 메시지 → OpenAI → (필요시) 함수 호출 → 최종 답변
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

    # 세션 히스토리 구성
    msgs = [{"role": "system", "content": SYSTEM_INSTRUCTIONS}]
    for t in get_session(session_id):
        msgs.append({"role": t["role"], "content": t["content"]})
    msgs.append({"role": "user", "content": user_msg})

    try:
        # 1차 응답(도구 사용 여부 판단)
        comp = client.chat.completions.create(
            model="gpt-5",
            messages=msgs,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = comp.choices[0].message

        # 도구 호출 시: 실행 결과를 재주입해 최종 응답 생성
        if getattr(msg, "tool_calls", None):
            tool_msgs = []
            for tc in msg.tool_calls:
                fn = tc.function.name
                args = json.loads(tc.function.arguments or "{}")
                result = run_tool(fn, args)
                tool_msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False)})

            final = client.chat.completions.create(
                model="gpt-5",
                messages=msgs + [msg] + tool_msgs
            )
            answer = final.choices[0].message.content or "응답 생성 실패"
        else:
            answer = msg.content or "응답 생성 실패"

        add_turn(session_id, "user", user_msg)
        add_turn(session_id, "assistant", answer)
        return {"answer": answer, "session_id": session_id}
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
@app.post("/api/tts")
def tts_google_post(payload: dict = Body(...)):
    text = (payload.get("text") or "").strip()
    lang = payload.get("lang") or "ko-KR"
    voice = payload.get("voice") or None
    fmt = payload.get("fmt") or "MP3"
    rate = float(payload.get("rate") or 1.0)
    pitch = float(payload.get("pitch") or 0.0)
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)

    # 서비스계정 키 경로 검증/자격 생성
    GCP_KEY_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not GCP_KEY_PATH or not os.path.exists(GCP_KEY_PATH):
        return JSONResponse({"error": "GCP 서비스계정 키 경로가 올바르지 않습니다."}, status_code=400)
    gcp_credentials = service_account.Credentials.from_service_account_file(
        GCP_KEY_PATH
    )

    tts_client = texttospeech.TextToSpeechClient(credentials=gcp_credentials)

    try:
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice_name = _pick_voice(lang, voice)
        voice_params = texttospeech.VoiceSelectionParams(
            language_code=lang, name=voice_name
        )

        if fmt == "MP3":
            audio_cfg = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=rate,
                pitch=pitch,
            )
            media_type, ext = "audio/mpeg", "mp3"
        elif fmt == "OGG_OPUS":
            audio_cfg = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.OGG_OPUS,
                speaking_rate=rate,
                pitch=pitch,
            )
            media_type, ext = "audio/ogg", "ogg"
        else:
            audio_cfg = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                speaking_rate=rate,
                pitch=pitch,
            )
            media_type, ext = "audio/wav", "wav"

        resp = tts_client.synthesize_speech(
            input=synthesis_input, voice=voice_params, audio_config=audio_cfg
        )
        headers = {
            "Content-Type": media_type,
            "Cache-Control": "no-cache",
            "Content-Disposition": f'inline; filename="speech.{ext}"',
        }
        return StreamingResponse(io.BytesIO(resp.audio_content), headers=headers)
    except Exception as e:
        log.exception("Google TTS 실패")
        return JSONResponse({"error": f"TTS 실패: {e}"}, status_code=500)

# =========================
# 유틸/헬스/스케줄러
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

# ===== 스케줄러 =====
# 네이버 크롤러 주기 실행 (10분) 테스트 후, 1시간 간격
scheduler = BackgroundScheduler(timezone=KST)

def _job_naver():
    try:
        from crawler_rag import crawl_today
        crawl_today(limit_per_run=50)
    except Exception as e:
        log.exception("네이버 수집 실패: %s", e)

@app.on_event("startup")
def _start_scheduler():
    # Mongo 인덱스 확인
    try:
        _ensure_indexes()
    except Exception as e:
        log.exception("인덱스 생성 실패")

    # 스케줄러 시작
    try:
        scheduler.add_job(
            _job_naver,
            "interval",
            minutes=10, # hours=1
            id="naver_hourly",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
        scheduler.start()
        log.info("APScheduler started.")
    except Exception:
        log.exception("APScheduler 시작 실패")

@app.on_event("shutdown")
def _stop_scheduler():
    try:
        scheduler.shutdown()
        log.info("APScheduler stopped.")
    except Exception:
        log.exception("APScheduler 종료 실패")