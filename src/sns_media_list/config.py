"""Application configuration and bounded runtime settings."""

from functools import lru_cache

from pydantic import Field
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
    trusted_proxy_cidrs: tuple[str, ...] = ()
    extraction_proxy_host: str = "127.0.0.1"
    extraction_proxy_port: int = Field(default=8765, ge=1, le=65535)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide validated settings instance."""
    return Settings()
