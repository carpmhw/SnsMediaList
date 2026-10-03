"""與 REST 私有 implementation 分離的 MCP 輸入及公開結果模型。"""

from typing import overload

from pydantic import BaseModel, ConfigDict, Field

from ..models import ExtractionResponse, MediaType, Platform


class McpExtractionInput(BaseModel):
    """單一 URL 的嚴格 tool input，不接受 credentials 或額外欄位。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    url: str = Field(min_length=1, max_length=2048)


class McpMediaItem(BaseModel):
    """只有公開 metadata 與可依明確 origin 投影的短效媒體連結。"""

    model_config = ConfigDict(extra="forbid")
    media_type: MediaType
    filename: str
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    duration: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    preview_url: str | None = None
    download_url: str


class McpExtractionResult(BaseModel):
    """穩定 structured result，不包含 upstream 或 token store 私有資料。"""

    model_config = ConfigDict(extra="forbid")
    platform: Platform
    post_url: str
    author: str | None = None
    description: str | None = None
    unavailable_media_count: int = Field(default=0, ge=0)
    media: list[McpMediaItem] = Field(max_length=20)


@overload
def _to_public_url(path: str, public_base_url: str | None) -> str:
    """必要 URL 經映射後仍維持字串型別。"""
    ...


@overload
def _to_public_url(path: None, public_base_url: str | None) -> None:
    """缺少的 preview URL 經映射後仍為 null。"""
    ...


def _to_public_url(path: str | None, public_base_url: str | None) -> str | None:
    """只串接 root-relative path，保留 null、絕對與 network-path reference。"""
    if path is None or public_base_url is None:
        return path
    if not path.startswith("/") or path.startswith("//"):
        return path
    return f"{public_base_url}{path}"


def to_mcp_result(
    result: ExtractionResponse, *, public_base_url: str | None = None
) -> McpExtractionResult:
    """逐欄映射公開 response，僅依經驗證的 origin 投影媒體連結。"""
    return McpExtractionResult(
        platform=result.platform,
        post_url=str(result.post_url),
        author=result.author,
        description=result.description,
        unavailable_media_count=result.unavailable_media_count,
        media=[
            McpMediaItem(
                media_type=item.media_type,
                filename=item.filename,
                width=item.width,
                height=item.height,
                duration=item.duration,
                preview_url=_to_public_url(item.preview_url, public_base_url),
                download_url=_to_public_url(item.download_url, public_base_url),
            )
            for item in result.media
        ],
    )
