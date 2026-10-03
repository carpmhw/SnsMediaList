"""MCP input 與公開 metadata adapter 的契約測試。"""

import pytest
from pydantic import ValidationError

from sns_media_list.models import ExtractionResponse, MediaItem


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"url": ""},
        {"url": "x" * 2049},
        {"url": 1},
        {"url": "https://x.com/a/status/1", "cookie": "secret"},
    ],
)
def test_mcp_input_is_strict(payload: dict[str, object]) -> None:
    """拒絕不符 tool schema 的資料，不接受額外憑證參數。"""
    from sns_media_list.mcp.models import McpExtractionInput

    with pytest.raises(ValidationError):
        McpExtractionInput.model_validate(payload)


def test_mcp_input_schema_has_required_url_only() -> None:
    """公開 input schema 必須完整表達長度與禁止多餘欄位。"""
    from sns_media_list.mcp.models import McpExtractionInput

    schema = McpExtractionInput.model_json_schema()
    assert schema["required"] == ["url"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"url"}
    assert schema["properties"]["url"]["minLength"] == 1
    assert schema["properties"]["url"]["maxLength"] == 2048


def test_mcp_mapper_preserves_public_metadata_and_relative_paths() -> None:
    """Mapper 保留順序、null 與相對連結，沒有私有 record 欄位。"""
    from sns_media_list.mcp.models import to_mcp_result

    public = ExtractionResponse(
        platform="x",
        post_url="https://x.com/example/status/1/",
        unavailable_media_count=2,
        media=[
            MediaItem(
                media_type="video",
                filename="x-example-1-01.mp4",
                download_url="/api/media/download-one/download",
                preview_url="/placeholder.svg",
            ),
            MediaItem(
                media_type="image",
                filename="x-example-1-02.jpg",
                width=1200,
                download_url="/api/media/download-two/download",
                preview_url="/api/media/preview-two/preview",
            ),
        ],
    )
    result = to_mcp_result(public).model_dump(mode="json")
    assert result["post_url"] == str(public.post_url)
    assert result["author"] is None
    assert result["unavailable_media_count"] == 2
    assert [item["filename"] for item in result["media"]] == [
        item.filename for item in public.media
    ]
    assert result["media"][0]["preview_url"] == "/placeholder.svg"
    assert result["media"][1]["width"] == 1200
    assert set(result["media"][0]) == {
        "media_type",
        "filename",
        "width",
        "height",
        "duration",
        "preview_url",
        "download_url",
    }


@pytest.mark.parametrize("base", [None, "http://192.168.50.14:8000", "https://sns.example.com"])
def test_mcp_mapper_projects_only_root_relative_links(base: str | None) -> None:
    """公開 origin 只投影媒體 path，保留 metadata、順序與原始 response。"""
    from sns_media_list.mcp.models import to_mcp_result

    public = ExtractionResponse(
        platform="x",
        post_url="https://x.com/example/status/1/",
        unavailable_media_count=2,
        media=[
            MediaItem(
                media_type="image",
                filename="one.jpg",
                width=1200,
                preview_url="/api/media/p/preview",
                download_url="/api/media/d/download",
            ),
            MediaItem(
                media_type="video",
                filename="two.mp4",
                preview_url="/placeholder.svg",
                download_url="/api/media/d2/download",
            ),
        ],
    )
    before = public.model_dump(mode="json")
    mapped = to_mcp_result(public, public_base_url=base).model_dump(mode="json")
    expected = public.model_dump(mode="json")
    if base is not None:
        for item in expected["media"]:
            item["preview_url"] = base + item["preview_url"]
            item["download_url"] = base + item["download_url"]
    assert mapped == expected
    assert public.model_dump(mode="json") == before


@pytest.mark.parametrize(
    "preview", [None, "https://already.example/media.jpg", "relative.jpg", "//already.example/a"]
)
@pytest.mark.parametrize(
    "download", ["https://already.example/media.mp4", "relative.mp4", "//already.example/b"]
)
def test_mcp_mapper_preserves_null_and_non_root_relative_links(
    preview: str | None, download: str
) -> None:
    """既有絕對或 network-path reference 不會被重新串接或解析。"""
    from sns_media_list.mcp.models import to_mcp_result

    public = ExtractionResponse(
        platform="x",
        post_url="https://x.com/example/status/1/",
        media=[
            MediaItem(
                media_type="video", filename="one.mp4", preview_url=preview, download_url=download
            )
        ],
    )
    item = to_mcp_result(public, public_base_url="https://sns.example.com").media[0]
    assert item.preview_url == preview
    assert item.download_url == download
