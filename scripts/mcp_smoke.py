"""以官方 Client 驗證 MCP 協定；預設不存取社群平台或回顯敏感結果。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from mcp import Client

from sns_media_list.logging_config import configure_mcp_logging


async def run_smoke(endpoint: str, *, mode: str = "auto", url: str | None = None) -> None:
    """驗證唯一 tool schema，只有顯式提供 URL 才執行擷取且僅輸出安全摘要。"""
    configure_mcp_logging()
    async with asyncio.timeout(60):
        async with Client(endpoint, mode=mode, read_timeout_seconds=55, cache=None) as client:
            tools = (await client.list_tools()).tools
            if len(tools) != 1 or tools[0].name != "extract_media":
                raise RuntimeError("unexpected MCP tools")
            schema = tools[0].input_schema
            field = schema.get("properties", {}).get("url", {})
            if (
                schema.get("required") != ["url"]
                or schema.get("additionalProperties") is not False
                or field.get("type") != "string"
                or field.get("minLength") != 1
                or field.get("maxLength") != 2048
                or tools[0].output_schema is None
            ):
                raise RuntimeError("unexpected MCP schema")
            if client.server_info is None or client.server_info.name != "SNS Media List":
                raise RuntimeError("unexpected MCP server")
            if url is not None:
                result = await client.call_tool("extract_media", {"url": url})
                if result.is_error or result.structured_content is None:
                    raise RuntimeError("MCP extraction smoke failed")
    print("MCP smoke 通過：SNS Media List／extract_media。")


def main(arguments: Sequence[str] | None = None) -> int:
    """讀取選用 owner-controlled 暫存 URL，失敗只輸出固定訊息而非原始例外。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint")
    parser.add_argument("--mode", choices=("auto", "legacy", "2026-07-28"), default="auto")
    parser.add_argument(
        "--url-file", type=Path, help="選用 owner-controlled URL 暫存檔；預設不擷取"
    )
    options = parser.parse_args(arguments)
    try:
        url = None
        if options.url_file is not None:
            if options.url_file.stat().st_size > 8192:
                raise ValueError("bounded URL file required")
            url = options.url_file.read_text(encoding="utf-8").strip()
            if not 1 <= len(url) <= 2048:
                raise ValueError("invalid URL input")
        asyncio.run(run_smoke(options.endpoint, mode=options.mode, url=url))
    except Exception:
        print("MCP smoke 失敗：請確認設定、協定連線與安全事件。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
