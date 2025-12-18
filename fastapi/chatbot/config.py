# config.py — Pydantic Settings 기반 설정 관리
"""
환경변수를 타입 안전하게 관리하는 설정 모듈.
.env 파일 또는 환경변수에서 자동 로드.
"""

import shutil
from functools import lru_cache
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator


class Settings(BaseSettings):
    """애플리케이션 전역 설정"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # 정의되지 않은 환경변수 무시
    )

    # ===== MongoDB =====
    mongo_uri: str = Field(
        default="mongodb://localhost:27017",
        description="MongoDB 연결 URI"
    )
    mongo_db_name: str = Field(
        default="local",
        description="MongoDB 데이터베이스 이름"
    )
    mongo_coll_name: str = Field(
        default="chatbot1_rag",
        description="MongoDB 컬렉션 이름"
    )

    # ===== API Keys =====
    fred_api_key: str = Field(
        default="",
        description="FRED API 키"
    )
    ecos_api_key: str = Field(
        default="",
        description="한국은행 ECOS API 키"
    )
    openai_api_key: Optional[str] = Field(
        default=None,
        description="OpenAI API 키 (백업용)"
    )

    # ===== CLOVA STT =====
    clova_key_id: str = Field(
        default="",
        description="CLOVA Speech API 클라이언트 ID"
    )
    clova_key: str = Field(
        default="",
        description="CLOVA Speech API 시크릿 키"
    )

    # ===== Google Cloud TTS =====
    google_application_credentials: Optional[str] = Field(
        default=None,
        description="Google Cloud 서비스 계정 JSON 파일 경로"
    )

    # ===== FFmpeg =====
    ffmpeg_bin: Optional[str] = Field(
        default=None,
        description="FFmpeg 바이너리 경로"
    )

    # ===== Vector Store =====
    vector_store_id: str = Field(
        default="",
        description="OpenAI Vector Store ID (백업용)"
    )

    # ===== Server =====
    cors_origins: str = Field(
        default="*",
        description="CORS 허용 origins (쉼표 구분)"
    )
    log_level: str = Field(
        default="INFO",
        description="로깅 레벨"
    )

    # ===== Ollama =====
    ollama_model: str = Field(
        default="gemma2:9b",
        description="Ollama 모델명"
    )
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description="Ollama 서버 URL"
    )
    ollama_temperature: float = Field(
        default=0.3,
        ge=0.0,
        le=2.0,
        description="LLM temperature"
    )
    ollama_num_ctx: int = Field(
        default=8192,
        ge=1024,
        le=32768,
        description="LLM context window 크기"
    )

    # ===== Cache =====
    quote_cache_ttl_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description="시세 캐시 TTL (초)"
    )

    @field_validator("ffmpeg_bin", mode="before")
    @classmethod
    def resolve_ffmpeg_path(cls, v: Optional[str]) -> str:
        """FFmpeg 경로 자동 탐색"""
        if v:
            return v
        # 자동 탐색
        found = shutil.which("ffmpeg")
        if found:
            return found
        # 일반적인 경로 시도
        for path in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]:
            import os
            if os.path.exists(path):
                return path
        return "/usr/bin/ffmpeg"  # 기본값

    @property
    def cors_origins_list(self) -> list[str]:
        """CORS origins를 리스트로 반환"""
        if self.cors_origins == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache()
def get_settings() -> Settings:
    """싱글톤 설정 인스턴스 반환 (캐싱)"""
    return Settings()


# 편의를 위한 전역 인스턴스
settings = get_settings()
