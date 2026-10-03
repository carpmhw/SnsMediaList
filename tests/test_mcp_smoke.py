"""MCP 協定 smoke 預設離線平台與輸出隱私的驗證。"""

from pathlib import Path
from typing import cast

import pytest

from sns_media_list.app import create_app
from sns_media_list.config import Settings
from sns_media_list.network.connect_proxy import ConnectProxy
from tests.api.test_mcp import FakeProxy
from tests.mcp_helpers import PRIVATE_SOURCE, X_URL, make_service, running_app


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_default_smoke_only_checks_protocol(
    mode: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """預設 smoke 不觸發 extractor、核發 token 或輸出 arguments。"""
    from scripts.mcp_smoke import run_smoke

    settings = Settings(mcp_enabled=True)
    service, extractor = make_service(settings=settings)
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy([])),
    )
    async with running_app(app) as origin:
        await run_smoke(f"{origin}/mcp", mode=mode)
    assert extractor.calls == 0
    assert service.token_store.size == 0
    assert "MCP smoke 通過" in capsys.readouterr().out


async def test_explicit_fake_extraction_does_not_print_token_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """顯式 owner-controlled URL 模式也不回顯 URL、upstream 或 token links。"""
    from scripts.mcp_smoke import run_smoke

    settings = Settings(mcp_enabled=True)
    service, extractor = make_service(settings=settings)
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy([])),
    )
    async with running_app(app) as origin:
        await run_smoke(f"{origin}/mcp", url=X_URL)
    assert extractor.calls == 1
    output = capsys.readouterr().out
    for value in (X_URL, PRIVATE_SOURCE, "/api/media/", "download_url"):
        assert value not in output


def test_smoke_invalid_input_file_reports_only_fixed_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """錯誤輸入不傾印檔案內容或 exception。"""
    from scripts.mcp_smoke import main

    path = tmp_path / "owner-url.txt"
    path.write_text("SENSITIVE" * 2000)
    assert main(["http://127.0.0.1:8000/mcp", "--url-file", str(path)]) == 1
    assert "SENSITIVE" not in capsys.readouterr().err
