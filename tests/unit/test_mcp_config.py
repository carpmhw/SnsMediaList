"""MCP 設定的預設值、環境載入與精確 allowlist 驗證。"""

import pytest
from pydantic import ValidationError

from sns_media_list.config import Settings


def test_mcp_defaults_are_disabled_and_bounded() -> None:
    """確認既有部署不會自動開啟 MCP 或接受無界 body。"""
    settings = Settings()
    assert settings.mcp_enabled is False
    assert settings.mcp_max_request_body_bytes == 65_536
    assert settings.mcp_allowed_hosts == ()
    assert settings.mcp_allowed_origins == ()
    assert settings.extraction_body_limit_bytes == 4_096


def test_mcp_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """確認 MCP 沿用 SNS_MEDIA 前綴與 JSON array 設定。"""
    monkeypatch.setenv("SNS_MEDIA_MCP_ENABLED", "true")
    monkeypatch.setenv("SNS_MEDIA_MCP_MAX_REQUEST_BODY_BYTES", "8192")
    monkeypatch.setenv("SNS_MEDIA_MCP_ALLOWED_HOSTS", '["mcp.example:8443","[::1]:8000"]')
    monkeypatch.setenv("SNS_MEDIA_MCP_ALLOWED_ORIGINS", '["https://mcp.example:8443"]')
    settings = Settings()
    assert settings.mcp_enabled is True
    assert settings.mcp_max_request_body_bytes == 8_192
    assert settings.mcp_allowed_hosts == ("mcp.example:8443", "[::1]:8000")
    assert settings.mcp_allowed_origins == ("https://mcp.example:8443",)


@pytest.mark.parametrize("limit", [0, -1, 1_048_577])
def test_mcp_body_limit_rejects_invalid_bounds(limit: int) -> None:
    """拒絕關閉或放寬為無界的 body 設定。"""
    with pytest.raises(ValidationError):
        Settings(mcp_max_request_body_bytes=limit)


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "mcp.example:*",
        "[abc].example",
        "mcp.example/path",
        "https://mcp.example",
        "user:password@mcp.example",
        "mcp.example?secret",
        "mcp.example#fragment",
        "mcp.example:0",
        "mcp.example:65536",
        "mcp.example:",
        "mcp.example\n",
        "mcp.example\\evil",
        "",
        "bad host",
        "[not-ipv6]",
    ],
)
def test_mcp_hosts_reject_non_exact_authorities(value: str) -> None:
    """拒絕 glob、憑證、路徑與畸形 authority。"""
    with pytest.raises(ValidationError):
        Settings(mcp_allowed_hosts=(value,))


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "https://mcp.example:*",
        "ftp://mcp.example",
        "https://user@mcp.example",
        "https://mcp.example/path",
        "https://mcp.example/",
        "https://mcp.example?query",
        "https://mcp.example#fragment",
        "null",
        "https://mcp.example\t",
        "https://",
    ],
)
def test_mcp_origins_reject_non_exact_origins(value: str) -> None:
    """Origin 只接受沒有路徑或任意比對規則的 HTTP(S) authority。"""
    with pytest.raises(ValidationError):
        Settings(mcp_allowed_origins=(value,))


def test_mcp_allowlist_capacity_is_bounded() -> None:
    """兩組 allowlist 都不能無限制增加狀態。"""
    hosts = tuple(f"host{index}.example" for index in range(33))
    with pytest.raises(ValidationError):
        Settings(mcp_allowed_hosts=hosts)
    with pytest.raises(ValidationError):
        Settings(mcp_allowed_origins=tuple(f"https://{host}" for host in hosts))


def test_enabled_mcp_requires_room_for_regular_post_and_cancel_reserve() -> None:
    """MCP POST limit 至少為二，才能保留一般處理與一次 legacy cancellation。"""
    with pytest.raises(ValidationError, match="cancellation"):
        Settings(mcp_enabled=True, rate_limit_extraction_attempts=1)


def test_custom_mcp_origins_require_explicit_hosts() -> None:
    """自訂 Origin 卻沒有 Host 時直接拒絕錯誤配置，而非啟動後永遠 421。"""
    with pytest.raises(ValidationError, match="hosts"):
        Settings(mcp_allowed_origins=("https://mcp.example",))


def test_public_base_url_is_optional_and_loaded_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公開 origin 預設未設定，環境載入後不自動啟用 MCP 或修改 allowlists。"""
    monkeypatch.delenv("SNS_MEDIA_PUBLIC_BASE_URL", raising=False)
    assert Settings().public_base_url is None
    monkeypatch.setenv("SNS_MEDIA_PUBLIC_BASE_URL", "https://public.example/")
    settings = Settings()
    assert settings.public_base_url == "https://public.example"
    assert settings.mcp_enabled is False
    assert settings.mcp_allowed_hosts == ()
    assert settings.mcp_allowed_origins == ()


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.168.50.14:8000",
        "https://sns-media.example.internal",
        "https://sns.example.internal:8443",
        "http://[2001:db8::10]:8000",
        "http://localhost:8000",
        "https://Public.Example:443",
    ],
)
@pytest.mark.parametrize("trailing_slash", ["", "/"])
def test_public_base_url_accepts_exact_origins(origin: str, trailing_slash: str) -> None:
    """只移除 root slash，保留明確 scheme、authority 大小寫與 port。"""
    assert Settings(public_base_url=origin + trailing_slash).public_base_url == origin


def test_public_base_url_and_mcp_allowlists_are_independent() -> None:
    """Incoming allowlists 與 outgoing origin 各自保持 operator 的設定。"""
    settings = Settings(
        public_base_url="https://public.example",
        mcp_allowed_hosts=("ingress.example:8443",),
        mcp_allowed_origins=("https://ingress.example:8443",),
    )
    assert settings.public_base_url == "https://public.example"
    assert settings.mcp_allowed_hosts == ("ingress.example:8443",)
    assert settings.mcp_allowed_origins == ("https://ingress.example:8443",)
    assert Settings(mcp_allowed_hosts=("ingress.example",)).public_base_url is None
    with pytest.raises(ValidationError, match="hosts"):
        Settings(
            public_base_url="https://public.example",
            mcp_allowed_origins=("https://ingress.example",),
        )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ftp://example.com",
        "file:///tmp/test",
        "example.com",
        "//example.com",
        "https://",
        "https:///example.com",
        "https://user:pass@example.com",
        "https://example.com@evil.example",
        "https://@example.com",
        "https://*.example.com",
        "https://example.com/path",
        "https://example.com/%2f",
        "https://example.com//",
        "https://example.com?x=1",
        "https://example.com?",
        "https://example.com/#fragment",
        "https://example.com/#",
        " https://example.com",
        "https://example.com ",
        "https://exa\tmple.com",
        "https://example.com\r\n",
        "https://example.com\x00",
        "https://example.com\x7f",
        "https://example.com\\evil",
        "https://例子.example",
        "https://%65xample.com",
        "https://bad_host.example",
        "https://example.com:",
        "https://example.com:abc",
        "https://example.com:0",
        "https://example.com:65536",
        "http://2001:db8::10:8000",
        "http://[not-ipv6]:8000",
        "http://[2001:db8::10",
    ],
)
def test_public_base_url_rejects_non_origins(value: str) -> None:
    """拒絕非 origin 與 parser 可能容忍的原始非法字元，不進行網路探測。"""
    with pytest.raises(ValidationError):
        Settings(public_base_url=value)
