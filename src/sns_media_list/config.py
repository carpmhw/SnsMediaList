"""Application configuration and bounded runtime settings."""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Store validated settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_prefix="SNS_MEDIA_", extra="ignore")

    app_name: str = "SNS Media List"
    environment: str = "production"
    media_limit: int = Field(default=20, gt=0, le=20)
    token_ttl_seconds: int = Field(default=600, gt=0, le=3600)
    token_capacity: int = Field(default=200, gt=0, le=5000)
    extraction_timeout_seconds: float = Field(default=45.0, gt=0, le=300)
    extraction_output_limit: int = Field(default=2_000_000, gt=0, le=20_000_000)
    max_download_bytes: int = Field(default=500_000_000, gt=0, le=5_000_000_000)
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    read_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    download_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    max_redirects: int = Field(default=3, ge=0, le=10)
    max_extractions: int = Field(default=1, gt=0, le=16)
    max_downloads: int = Field(default=4, gt=0, le=32)
    thumbnail_input_bytes: int = Field(default=32_000_000, gt=0, le=32_000_000)
    thumbnail_output_bytes: int = Field(default=1_000_000, gt=0, le=1_000_000)
    thumbnail_timeout_seconds: float = Field(default=10.0, gt=0, le=10.0)
    thumbnail_concurrency: int = Field(default=1, gt=0, le=1)
    thumbnail_cache_bytes: int = Field(default=32_000_000, gt=0, le=32_000_000)
    thumbnail_max_edge: int = Field(default=640, gt=0, le=640)
    trusted_proxy_cidrs: tuple[str, ...] = ()
    extraction_proxy_host: str = "127.0.0.1"
    extraction_proxy_port: int = Field(default=8765, ge=1, le=65535)
    instagram_cookie_file: str | None = None
    x_cookie_file: str | None = None

    @field_validator("instagram_cookie_file", "x_cookie_file")
    @classmethod
    def validate_cookie_file(cls, value: str | None) -> str | None:
        """Validate an optional absolute, readable, non-empty cookie file."""
        if value is None:
            return None
        path = Path(value)
        try:
            valid = (
                path.is_absolute()
                and path.is_file()
                and os.access(path, os.R_OK)
                and path.stat().st_size > 0
            )
        except OSError:
            valid = False
        if not valid:
            raise ValueError("cookie file must be an absolute readable non-empty regular file")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide validated settings instance."""
    return Settings()
