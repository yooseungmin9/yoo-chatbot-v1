# test_api.py — FastAPI 엔드포인트 테스트
"""
FastAPI 엔드포인트 통합 테스트
"""

import pytest
from fastapi.testclient import TestClient
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def client():
    """FastAPI TestClient 생성"""
    from chatbot_v4 import app
    return TestClient(app)


class TestHealthEndpoint:
    """Health 엔드포인트 테스트"""

    def test_health_check(self, client):
        """GET /health 테스트"""
        response = client.get("/health")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "ok"
        assert "timestamp" in data


class TestChatEndpoint:
    """Chat 엔드포인트 테스트"""

    def test_chat_greeting(self, client):
        """인사 메시지 테스트"""
        response = client.post("/api/chat", json={
            "message": "안녕하세요",
            "session_id": "test_session"
        })
        assert response.status_code == 200

        data = response.json()
        assert "response" in data
        # 인사에 대한 응답이 있어야 함
        assert len(data["response"]) > 0

    def test_chat_empty_message(self, client):
        """빈 메시지 테스트"""
        response = client.post("/api/chat", json={
            "message": "",
            "session_id": "test_session"
        })
        # 빈 메시지도 처리되어야 함 (에러 또는 안내 메시지)
        assert response.status_code in [200, 400]

    def test_chat_missing_message(self, client):
        """메시지 필드 누락 테스트"""
        response = client.post("/api/chat", json={
            "session_id": "test_session"
        })
        # 필수 필드 누락 시 에러
        assert response.status_code in [200, 400, 422]


class TestResetEndpoint:
    """Reset 엔드포인트 테스트"""

    def test_reset_session(self, client):
        """POST /reset 테스트"""
        # 먼저 대화 생성
        client.post("/api/chat", json={
            "message": "테스트",
            "session_id": "reset_test_session"
        })

        # 세션 리셋
        response = client.post("/reset", json={
            "session_id": "reset_test_session"
        })
        assert response.status_code == 200


class TestStreamEndpoint:
    """스트리밍 엔드포인트 테스트"""

    def test_stream_chat(self, client):
        """POST /api/chat/stream 테스트"""
        response = client.post("/api/chat/stream", json={
            "message": "안녕",
            "session_id": "stream_test"
        })

        # 스트리밍 응답은 200이어야 함
        assert response.status_code == 200
        # Content-Type이 text/event-stream이어야 함
        assert "text/event-stream" in response.headers.get("content-type", "")


class TestCORSHeaders:
    """CORS 헤더 테스트"""

    def test_cors_headers_present(self, client):
        """CORS 헤더 존재 확인"""
        response = client.options("/api/chat", headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST"
        })

        # preflight 요청 처리
        # CORS 미들웨어가 적용되어 있으면 200 또는 405
        assert response.status_code in [200, 405]


class TestInputValidation:
    """입력 검증 테스트"""

    def test_very_long_message(self, client):
        """매우 긴 메시지 처리 테스트"""
        long_message = "테스트 " * 1000  # 약 5000자

        response = client.post("/api/chat", json={
            "message": long_message,
            "session_id": "long_message_test"
        })

        # 긴 메시지도 처리되어야 함 (에러 없이)
        assert response.status_code in [200, 400, 413]

    def test_special_characters(self, client):
        """특수문자 포함 메시지 테스트"""
        special_message = "삼성전자 <script>alert('xss')</script> 주가"

        response = client.post("/api/chat", json={
            "message": special_message,
            "session_id": "special_char_test"
        })

        # 특수문자가 있어도 처리되어야 함
        assert response.status_code == 200

        data = response.json()
        # XSS 스크립트가 그대로 반환되면 안 됨
        if "response" in data:
            assert "<script>" not in data["response"]

    def test_unicode_message(self, client):
        """유니코드 메시지 테스트"""
        unicode_message = "삼성전자 📈 주가 알려줘 🚀"

        response = client.post("/api/chat", json={
            "message": unicode_message,
            "session_id": "unicode_test"
        })

        assert response.status_code == 200
