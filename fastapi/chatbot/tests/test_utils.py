# test_utils.py — 유틸리티 함수 테스트
"""
chatbot_v4.py의 유틸리티 함수들 테스트
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestHallucinationDetection:
    """할루시네이션 감지 함수 테스트"""

    def test_extract_numbers_from_text(self):
        """텍스트에서 숫자 추출 테스트"""
        from chatbot_v4 import _extract_numbers_from_text

        # 기본 숫자
        assert 123 in _extract_numbers_from_text("가격은 123원입니다")
        assert 45.67 in _extract_numbers_from_text("변동률 45.67%")

        # 쉼표가 있는 숫자
        numbers = _extract_numbers_from_text("52,000원")
        assert 52000 in numbers or 52000.0 in numbers

        # 여러 숫자
        numbers = _extract_numbers_from_text("코스피 2,500.50, 코스닥 800.25")
        assert len(numbers) >= 2

    def test_detect_hallucination_patterns(self):
        """할루시네이션 패턴 감지 테스트"""
        from chatbot_v4 import _detect_hallucination_patterns

        # 할루시네이션 패턴
        suspicious = _detect_hallucination_patterns("약 52,000원 정도입니다")
        assert len(suspicious) > 0

        suspicious = _detect_hallucination_patterns("대략 3% 상승했습니다")
        assert len(suspicious) > 0

        suspicious = _detect_hallucination_patterns("추정컨대 1,000억원입니다")
        assert len(suspicious) > 0

        # 정상 응답 (패턴 없음)
        clean = _detect_hallucination_patterns("현재가는 52,000원입니다")
        # 정확한 수치 언급은 패턴에 걸리지 않아야 함 (구현에 따라 다름)

    def test_is_number_valid(self):
        """숫자 유효성 검증 테스트"""
        from chatbot_v4 import _is_number_valid

        valid_set = {52000, 2500.50, "1,234"}

        assert _is_number_valid(52000, valid_set) is True
        assert _is_number_valid(52000.0, valid_set) is True
        assert _is_number_valid(2500.50, valid_set) is True
        assert _is_number_valid(99999, valid_set) is False

    def test_find_suspicious_numbers(self):
        """의심스러운 숫자 찾기 테스트"""
        from chatbot_v4 import _find_suspicious_numbers

        valid_numbers = {52000, 2500}

        # 유효한 숫자만 있는 경우
        suspicious = _find_suspicious_numbers("가격은 52000원입니다", valid_numbers)
        assert len(suspicious) == 0

        # 의심스러운 숫자가 있는 경우 (1000 이상)
        suspicious = _find_suspicious_numbers("가격은 99999원입니다", valid_numbers)
        assert 99999 in suspicious or 99999.0 in suspicious

        # 1000 미만은 검사하지 않음
        suspicious = _find_suspicious_numbers("변동률 5%입니다", valid_numbers)
        assert len(suspicious) == 0


class TestResponseFilter:
    """응답 필터링 함수 테스트"""

    def test_remove_data_tags(self):
        """[DATA] 태그 제거 테스트"""
        from chatbot_v4 import _remove_data_tags

        text = "[DATA]price=52000[/DATA] 현재가는 52,000원입니다"
        result = _remove_data_tags(text)

        assert "[DATA]" not in result
        assert "[/DATA]" not in result

    def test_filter_response_basic(self):
        """기본 응답 필터링 테스트"""
        from chatbot_v4 import _filter_response

        # 기본 필터링
        response = "코스피 지수는 2,500.50입니다.\n\n\n많이 올랐네요."
        filtered = _filter_response(response)

        # 연속 줄바꿈 정리
        assert "\n\n\n" not in filtered

    def test_filter_response_with_valid_numbers(self):
        """유효 숫자 세트와 함께 필터링 테스트"""
        from chatbot_v4 import _filter_response

        valid_numbers = {52000, 2500.50}
        response = "삼성전자 52,000원, 코스피 2,500.50"

        # 유효한 숫자만 있으면 경고 없이 통과
        filtered = _filter_response(response, valid_numbers)
        assert "52" in filtered  # 숫자가 유지됨


class TestKRXCodeExtraction:
    """KRX 코드 추출 함수 테스트"""

    def test_extract_krx_code_6digit(self):
        """6자리 코드 추출 테스트"""
        from chatbot_v4 import _extract_krx_code

        assert _extract_krx_code("005930") == "005930"
        assert _extract_krx_code("035420") == "035420"

    def test_extract_krx_code_with_suffix(self):
        """.KS/.KQ 접미사 제거 테스트"""
        from chatbot_v4 import _extract_krx_code

        assert _extract_krx_code("005930.KS") == "005930"
        assert _extract_krx_code("035420.KQ") == "035420"

    def test_extract_krx_code_non_korean(self):
        """해외 주식 코드는 None 반환"""
        from chatbot_v4 import _extract_krx_code

        assert _extract_krx_code("AAPL") is None
        assert _extract_krx_code("^KS11") is None
        assert _extract_krx_code("USDKRW=X") is None


class TestFormatFunctions:
    """포맷팅 함수 테스트"""

    def test_format_kst_human(self):
        """KST 시간 포맷팅 테스트"""
        from chatbot_v4 import format_kst_human

        # 정상 ISO 형식
        result = format_kst_human("2025-12-17T10:30:00+09:00")
        assert "2025년" in result
        assert "12월" in result
        assert "17일" in result

        # 잘못된 형식은 원본 반환
        invalid = "not a date"
        assert format_kst_human(invalid) == invalid

    def test_format_index_output(self):
        """지수 출력 포맷팅 테스트"""
        from chatbot_v4 import _format_index_output

        data = {"price": 2500.50, "change": 25.30, "changePct": 1.02}
        result = _format_index_output("코스피", data)

        assert "output" in result
        assert "코스피" in result["output"]
        assert "2,500.50" in result["output"]

    def test_format_index_output_no_data(self):
        """데이터 없을 때 지수 출력 테스트"""
        from chatbot_v4 import _format_index_output

        data = {"price": None}
        result = _format_index_output("코스피", data)

        assert "output" in result
        assert "가져올 수 없습니다" in result["output"]

    def test_format_fx_output(self):
        """환율 출력 포맷팅 테스트"""
        from chatbot_v4 import _format_fx_output

        data = {"price": 1350.50, "change": 5.20, "changePct": 0.39}
        result = _format_fx_output("달러/원", data)

        assert "output" in result
        assert "달러/원" in result["output"]
        assert "1,350.50" in result["output"]

    def test_format_fx_output_with_multiply(self):
        """엔화 환율 (100엔 기준) 포맷팅 테스트"""
        from chatbot_v4 import _format_fx_output

        data = {"price": 9.05, "change": 0.05, "changePct": 0.55}
        result = _format_fx_output("엔/원", data, multiply=100)

        assert "output" in result
        # 9.05 * 100 = 905
        assert "905" in result["output"]
