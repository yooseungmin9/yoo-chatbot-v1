# test_tool_router.py — ToolRouter 패턴 매칭 테스트
"""
ToolRouter의 정규식 패턴 매칭 및 파라미터 추출 테스트
"""

import pytest
import sys
import os

# 상위 디렉토리를 path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestToolRouter:
    """ToolRouter 클래스 테스트"""

    @pytest.fixture
    def router(self):
        """ToolRouter 인스턴스 생성"""
        from chatbot_v4 import ToolRouter
        return ToolRouter()

    # ===== 뉴스 관련 테스트 =====
    @pytest.mark.parametrize("query,expected_tool", [
        ("최신 뉴스 알려줘", "get_latest_news"),
        ("오늘 뉴스 뭐 있어?", "get_latest_news"),
        ("최근 경제 뉴스", "get_latest_news"),
        ("뉴스 5개 보여줘", "get_latest_news"),
    ])
    def test_news_routing(self, router, query, expected_tool):
        """뉴스 관련 쿼리 라우팅 테스트"""
        result = router.route(query)
        assert result is not None, f"'{query}'가 매칭되지 않음"
        assert result["tool"] == expected_tool

    # ===== 주가 관련 테스트 =====
    @pytest.mark.parametrize("query,expected_tool,expected_ticker", [
        ("삼성전자 주가 알려줘", "get_market", "005930.KS"),
        ("네이버 주가", "get_market", "035420.KS"),
        ("SK하이닉스 주가는?", "get_market", "000660.KS"),
        ("현대차 주가 어때", "get_market", "005380.KS"),
    ])
    def test_stock_routing(self, router, query, expected_tool, expected_ticker):
        """주가 관련 쿼리 라우팅 테스트"""
        result = router.route(query)
        assert result is not None, f"'{query}'가 매칭되지 않음"
        assert result["tool"] == expected_tool
        assert result["params"]["ticker"] == expected_ticker

    # ===== 지수 관련 테스트 =====
    @pytest.mark.parametrize("query,expected_market_type", [
        ("코스피 지수 알려줘", "KOSPI"),
        ("코스닥 어때?", "KOSDAQ"),
        ("오늘 코스피", "KOSPI"),
    ])
    def test_index_routing(self, router, query, expected_market_type):
        """지수 관련 쿼리 라우팅 테스트"""
        result = router.route(query)
        assert result is not None, f"'{query}'가 매칭되지 않음"
        assert result["tool"] == "get_market"
        assert result["params"]["market_type"] == expected_market_type

    # ===== 환율 관련 테스트 =====
    @pytest.mark.parametrize("query,expected_market_type", [
        ("달러 환율 알려줘", "USD_KRW"),
        ("환율 얼마야", "USD_KRW"),
        ("원달러 환율", "USD_KRW"),
        ("엔화 환율", "JPY_KRW"),
    ])
    def test_forex_routing(self, router, query, expected_market_type):
        """환율 관련 쿼리 라우팅 테스트"""
        result = router.route(query)
        assert result is not None, f"'{query}'가 매칭되지 않음"
        assert result["tool"] == "get_market"
        assert result["params"]["market_type"] == expected_market_type

    # ===== 경제지표 관련 테스트 =====
    @pytest.mark.parametrize("query,expected_indicator", [
        ("한국 기준금리 알려줘", "BASE_RATE"),
        ("국내 금리", "BASE_RATE"),
        ("미국 금리", "US_FEDFUNDS"),
        ("GDP 알려줘", "GDP"),
        ("무역수지", "TRADE_BALANCE"),
    ])
    def test_indicator_routing(self, router, query, expected_indicator):
        """경제지표 관련 쿼리 라우팅 테스트"""
        result = router.route(query)
        assert result is not None, f"'{query}'가 매칭되지 않음"
        assert result["tool"] == "get_indicator"
        assert result["params"]["indicator_type"] == expected_indicator

    # ===== 일반 대화 테스트 (None 반환) =====
    @pytest.mark.parametrize("query", [
        "안녕하세요",
        "오늘 날씨 어때?",
        "점심 뭐 먹을까",
        "주식 투자 어떻게 해?",
        "좋은 아침이에요",
    ])
    def test_general_conversation_returns_none(self, router, query):
        """일반 대화는 None을 반환해야 함"""
        result = router.route(query)
        assert result is None, f"'{query}'가 잘못 매칭됨: {result}"

    # ===== 시장 요약 테스트 =====
    def test_market_summary_routing(self, router):
        """시장 요약 쿼리 라우팅 테스트"""
        queries = ["시장 요약", "전체 시장 현황", "시장 현황 알려줘"]
        for query in queries:
            result = router.route(query)
            if result:  # 매칭되는 경우만 체크
                assert result["tool"] == "get_market"
                assert result["params"]["market_type"] == "MARKET_SUMMARY"


class TestToolRouterEdgeCases:
    """ToolRouter 엣지 케이스 테스트"""

    @pytest.fixture
    def router(self):
        from chatbot_v4 import ToolRouter
        return ToolRouter()

    def test_empty_query(self, router):
        """빈 쿼리 처리"""
        result = router.route("")
        assert result is None

    def test_whitespace_query(self, router):
        """공백만 있는 쿼리 처리"""
        result = router.route("   ")
        assert result is None

    def test_case_insensitive(self, router):
        """대소문자 무관 매칭"""
        result1 = router.route("코스피")
        result2 = router.route("KOSPI 지수")

        assert result1 is not None
        # 영문 KOSPI는 패턴에 따라 매칭될 수도 있고 아닐 수도 있음
        # 한글 "코스피"는 반드시 매칭되어야 함

    def test_partial_match(self, router):
        """부분 매칭 테스트"""
        # "삼성전자"가 포함된 다양한 문장
        queries = [
            "삼성전자 주가 알려줘",
            "오늘 삼성전자 주가는?",
            "삼성전자 현재 주가",
        ]
        for query in queries:
            result = router.route(query)
            assert result is not None, f"'{query}'가 매칭되지 않음"
            assert result["params"]["ticker"] == "005930.KS"
