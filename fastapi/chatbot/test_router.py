#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
규칙 기반 라우터 테스트 스크립트
chatbot-v4.py 실행 없이 ToolRouter 단독 테스트
"""

import sys
import re
from typing import Dict, Any, Optional

# ===== 티커 매핑 (chatbot-v4.py에서 복사) =====
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

# ===== 규칙 기반 도구 라우터 (chatbot-v4.py에서 복사) =====
class ToolRouter:
    """규칙 기반 도구 선택 및 파라미터 추출"""

    def __init__(self):
        self.rules = [
            # 뉴스 관련
            (r'(최신|최근|오늘|어제).{0,5}뉴스', 'get_latest_news', self._extract_news_params),
            (r'뉴스.{0,5}(\d+)개', 'get_latest_news', self._extract_news_params),

            # 주가 관련 (한국 종목명 우선)
            (r'(삼성전자|네이버|SK하이닉스|카카오|현대차|기아|LG에너지|포스코|셀트리온).{0,5}주가', 'get_market', self._extract_stock_params),
            (r'주가.{0,5}(삼성전자|네이버|SK하이닉스|카카오|현대차|기아|LG에너지|포스코|셀트리온)', 'get_market', self._extract_stock_params),

            # 지수 관련
            (r'코스피', 'get_market', lambda q: {'market_type': 'KOSPI', 'ticker': ''}),
            (r'코스닥', 'get_market', lambda q: {'market_type': 'KOSDAQ', 'ticker': ''}),
            (r'KOSPI', 'get_market', lambda q: {'market_type': 'KOSPI', 'ticker': ''}),
            (r'KOSDAQ', 'get_market', lambda q: {'market_type': 'KOSDAQ', 'ticker': ''}),

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

            # 서비스 도움말
            (r'(사용법|도움말|메뉴얼|가이드|사용방법)', 'search_docs', self._extract_docs_params),
        ]

    def _extract_news_params(self, query: str) -> dict:
        """뉴스 개수 추출"""
        match = re.search(r'(\d+)개', query)
        count = int(match.group(1)) if match else 5
        count = max(1, min(20, count))
        return {'count': count}

    def _extract_stock_params(self, query: str) -> dict:
        """주식 종목명 추출 및 티커 변환"""
        for name, ticker in KOREAN_TICKER_MAP.items():
            if name in query:
                return {'market_type': 'QUOTE', 'ticker': ticker}

        match = re.search(r'(\d{6}|[A-Z]{1,5})', query)
        if match:
            ticker = match.group(1)
            if re.match(r'^\d{6}$', ticker):
                ticker = f"{ticker}.KS"
            return {'market_type': 'QUOTE', 'ticker': ticker}

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
                    print(f"파라미터 추출 실패 ({pattern}): {e}")
                    continue

        return None

# ===== 테스트 케이스 =====
TEST_CASES = [
    # 뉴스
    ("최신 뉴스 알려줘", "get_latest_news", {'count': 5}),
    ("오늘 뉴스 10개", "get_latest_news", {'count': 10}),

    # 주가
    ("삼성전자 주가", "get_market", {'market_type': 'QUOTE', 'ticker': '005930.KS'}),
    ("네이버 주가 알려줘", "get_market", {'market_type': 'QUOTE', 'ticker': '035420.KS'}),
    ("카카오 주가는?", "get_market", {'market_type': 'QUOTE', 'ticker': '035720.KS'}),

    # 지수
    ("코스피 알려줘", "get_market", {'market_type': 'KOSPI', 'ticker': ''}),
    ("코스닥 지수", "get_market", {'market_type': 'KOSDAQ', 'ticker': ''}),

    # 환율
    ("달러 환율", "get_market", {'market_type': 'USD_KRW', 'ticker': ''}),
    ("엔화 환율", "get_market", {'market_type': 'JPY_KRW', 'ticker': ''}),

    # 경제지표
    ("한국 기준금리", "get_indicator", {'indicator_type': 'BASE_RATE'}),
    ("국내 GDP", "get_indicator", {'indicator_type': 'GDP'}),
    ("미국 금리", "get_indicator", {'indicator_type': 'US_FEDFUNDS'}),
    ("무역수지 알려줘", "get_indicator", {'indicator_type': 'TRADE_BALANCE'}),

    # 도움말
    ("사용법 알려줘", "search_docs", {'query': '사용법 알려줘'}),

    # 일반 대화 (매칭 안 됨)
    ("안녕하세요", None, None),
    ("주식 투자 어떻게 해?", None, None),
]

def test_router():
    """라우터 테스트 실행"""
    router = ToolRouter()

    passed = 0
    failed = 0

    print("=" * 80)
    print("규칙 기반 라우터 테스트")
    print("=" * 80)

    for i, (query, expected_tool, expected_params) in enumerate(TEST_CASES, 1):
        result = router.route(query)

        # 예상 결과와 비교
        if expected_tool is None:
            # 일반 대화 케이스
            if result is None:
                status = "✅ PASS"
                passed += 1
            else:
                status = f"❌ FAIL (expected: None, got: {result['tool']})"
                failed += 1
        else:
            # 도구 호출 케이스
            if result and result['tool'] == expected_tool and result['params'] == expected_params:
                status = "✅ PASS"
                passed += 1
            else:
                status = f"❌ FAIL"
                failed += 1
                if result:
                    print(f"   Expected: {expected_tool}({expected_params})")
                    print(f"   Got:      {result['tool']}({result['params']})")
                else:
                    print(f"   Expected: {expected_tool}({expected_params})")
                    print(f"   Got:      None")

        print(f"{i:2d}. {status:10s} | {query:30s} → {result}")

    print("=" * 80)
    print(f"테스트 결과: {passed} PASS, {failed} FAIL (총 {passed + failed}개)")
    print("=" * 80)

    return failed == 0

if __name__ == "__main__":
    success = test_router()
    sys.exit(0 if success else 1)
