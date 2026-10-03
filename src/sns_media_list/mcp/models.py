"""與 REST 私有 implementation 分離的 MCP 輸入及公開結果模型。"""

from pydantic import BaseModel, ConfigDict, Field

from ..models import ExtractionResponse, MediaType, Platform


class McpExtractionInput(BaseModel):
    """單一 URL 的嚴格 tool input，不接受 credentials 或額外欄位。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    url: str = Field(min_length=1, max_length=2048)


class McpMediaItem(BaseModel):
    """只有公開 metadata 與短效相對媒體連結的結果項目。"""

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


def to_mcp_result(result: ExtractionResponse) -> McpExtractionResult:
    """逐欄映射公開 response，避免序列化服務或私有 record。"""
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
                preview_url=item.preview_url,
                download_url=item.download_url,
            )
            for item in result.media
        ],
    )
