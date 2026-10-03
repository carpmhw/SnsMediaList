"""Application configuration and bounded runtime settings."""

import ipaddress
import os
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
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
    extraction_body_limit_bytes: int = Field(default=4_096, gt=0, le=65_536)
    max_download_bytes: int = Field(default=500_000_000, gt=0, le=5_000_000_000)
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    read_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    media_response_timeout_seconds: float = Field(default=120.0, gt=0, le=3_600)
    max_redirects: int = Field(default=3, ge=0, le=10)
    max_extractions: int = Field(default=1, gt=0, le=16)
    max_downloads: int = Field(default=4, gt=0, le=32)
    max_downloads_per_client: int = Field(default=2, gt=0, le=32)
    rate_limit_window_seconds: float = Field(default=60.0, gt=0, le=3_600)
    rate_limit_extraction_attempts: int = Field(default=10, gt=0, le=10)
    rate_limit_media_attempts: int = Field(default=120, gt=0, le=120)
    rate_limit_identity_capacity: int = Field(default=2_048, gt=0, le=2_048)
    generated_previews_enabled: bool = False
    mcp_enabled: bool = False
    mcp_max_request_body_bytes: int = Field(default=65_536, gt=0, le=1_048_576)
    mcp_allowed_hosts: tuple[str, ...] = Field(default=(), max_length=32)
    mcp_allowed_origins: tuple[str, ...] = Field(default=(), max_length=32)
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

    @field_validator("mcp_allowed_hosts")
    @classmethod
    def validate_mcp_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """只接受精確 authority，不允許 wildcard、憑證或 URL 路徑。"""
        for authority in value:
            _validate_mcp_authority(authority)
        return value

    @field_validator("mcp_allowed_origins")
    @classmethod
    def validate_mcp_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """限定精確 HTTP(S) origin，避免意外接受任意瀏覽器來源。"""
        for origin in value:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or origin != (
                f"{parsed.scheme}://{parsed.netloc}"
            ):
                raise ValueError("MCP origins must be exact HTTP(S) origins")
            _validate_mcp_authority(parsed.netloc)
        return value

    @model_validator(mode="after")
    def validate_mcp_cancellation_budget(self) -> "Settings":
        """驗證 MCP 取消額度與自訂 allowlist 間的必要相依。"""
        if self.mcp_enabled and self.rate_limit_extraction_attempts < 2:
            raise ValueError(
                "MCP requires at least two extraction attempts for POST and cancellation budgets"
            )
        if self.mcp_allowed_origins and not self.mcp_allowed_hosts:
            raise ValueError("custom MCP origins require explicit allowed hosts")
        return self

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def validate_trusted_proxy_cidrs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject malformed or globally trusted proxy networks."""
        for cidr in value:
            try:
                network = ipaddress.ip_network(cidr, strict=False)
            except ValueError as error:
                raise ValueError("trusted proxy CIDRs must be valid networks") from error
            if network.prefixlen == 0:
                raise ValueError("trusted proxy CIDRs cannot be global networks")
        return value

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


def _validate_mcp_authority(value: str) -> None:
    """驗證 ASCII DNS／IP authority 與可選明確 port，不建立網路連線。"""
    if (
        not value
        or len(value) > 260
        or any(ord(character) <= 32 or ord(character) >= 127 for character in value)
        or any(character in value for character in "*?/#@%\\")
        or value.endswith(":")
    ):
        raise ValueError("MCP hosts must be exact host authorities")
    parsed = urlsplit(f"//{value}")
    host, port = parsed.hostname, parsed.port
    if not host or parsed.netloc != value or port is not None and not 1 <= port <= 65535:
        raise ValueError("MCP hosts must be exact host authorities")
    if ":" in host:
        ipaddress.IPv6Address(host)
    elif not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
        raise ValueError("MCP hosts must be exact DNS or IP hosts")
