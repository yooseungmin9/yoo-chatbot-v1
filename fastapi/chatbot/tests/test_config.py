# test_config.py — Pydantic Settings 테스트
"""
config.py 모듈의 설정 로드 및 검증 테스트
"""

import pytest
import os
from unittest.mock import patch


class TestSettings:
    """Settings 클래스 테스트"""

    def test_default_values(self):
        """기본값 로드 테스트"""
        # 환경변수 없이 Settings 생성
        with patch.dict(os.environ, {}, clear=True):
            from config import Settings
            s = Settings()

            assert s.mongo_uri == "mongodb://localhost:27017"
            assert s.mongo_db_name == "local"
            assert s.mongo_coll_name == "chatbot1_rag"
            assert s.fred_api_key == ""
            assert s.ecos_api_key == ""
            assert s.ollama_model == "gemma2:9b"
            assert s.ollama_temperature == 0.3
            assert s.quote_cache_ttl_seconds == 30

    def test_env_override(self):
        """환경변수로 기본값 오버라이드 테스트"""
        env_vars = {
            "MONGO_URI": "mongodb://testhost:27017",
            "MONGO_DB_NAME": "test_db",
            "FRED_API_KEY": "test_fred_key",
            "ECOS_API_KEY": "test_ecos_key",
            "OLLAMA_MODEL": "llama3:8b",
            "OLLAMA_TEMPERATURE": "0.7",
        }

        with patch.dict(os.environ, env_vars, clear=True):
            from config import Settings
            s = Settings()

            assert s.mongo_uri == "mongodb://testhost:27017"
            assert s.mongo_db_name == "test_db"
            assert s.fred_api_key == "test_fred_key"
            assert s.ecos_api_key == "test_ecos_key"
            assert s.ollama_model == "llama3:8b"
            assert s.ollama_temperature == 0.7

    def test_cors_origins_list_single(self):
        """CORS origins 단일 값 테스트"""
        with patch.dict(os.environ, {"CORS_ORIGINS": "*"}, clear=True):
            from config import Settings
            s = Settings()
            assert s.cors_origins_list == ["*"]

    def test_cors_origins_list_multiple(self):
        """CORS origins 복수 값 테스트"""
        with patch.dict(os.environ, {"CORS_ORIGINS": "http://localhost:3000,http://localhost:8080"}, clear=True):
            from config import Settings
            s = Settings()
            assert s.cors_origins_list == ["http://localhost:3000", "http://localhost:8080"]

    def test_temperature_bounds(self):
        """temperature 범위 검증 테스트"""
        from config import Settings
        from pydantic import ValidationError

        # 유효 범위
        with patch.dict(os.environ, {"OLLAMA_TEMPERATURE": "0.0"}, clear=True):
            s = Settings()
            assert s.ollama_temperature == 0.0

        with patch.dict(os.environ, {"OLLAMA_TEMPERATURE": "2.0"}, clear=True):
            s = Settings()
            assert s.ollama_temperature == 2.0

        # 범위 초과
        with patch.dict(os.environ, {"OLLAMA_TEMPERATURE": "3.0"}, clear=True):
            with pytest.raises(ValidationError):
                Settings()

    def test_cache_ttl_bounds(self):
        """캐시 TTL 범위 검증 테스트"""
        from config import Settings
        from pydantic import ValidationError

        # 유효 범위
        with patch.dict(os.environ, {"QUOTE_CACHE_TTL_SECONDS": "5"}, clear=True):
            s = Settings()
            assert s.quote_cache_ttl_seconds == 5

        # 범위 미만
        with patch.dict(os.environ, {"QUOTE_CACHE_TTL_SECONDS": "1"}, clear=True):
            with pytest.raises(ValidationError):
                Settings()

    def test_ffmpeg_path_resolution(self):
        """FFmpeg 경로 자동 탐색 테스트"""
        from config import Settings

        # 명시적 경로 설정
        with patch.dict(os.environ, {"FFMPEG_BIN": "/custom/path/ffmpeg"}, clear=True):
            s = Settings()
            assert s.ffmpeg_bin == "/custom/path/ffmpeg"

        # 자동 탐색 (환경변수 없을 때)
        with patch.dict(os.environ, {}, clear=True):
            s = Settings()
            # ffmpeg_bin이 None이 아닌 문자열이어야 함
            assert isinstance(s.ffmpeg_bin, str)
            assert len(s.ffmpeg_bin) > 0
