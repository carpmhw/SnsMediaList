"""驗證原始媒體檢視的 Range 與被動格式邊界。"""

import asyncio

import pytest

from sns_media_list.errors import AppError
from sns_media_list.network.media_client import MediaClient, MediaDestinationPolicy, MediaResponse
from sns_media_list.network.media_viewing import (
    build_view_headers,
    parse_view_range,
    validate_view_response,
    validate_view_signature,
)
from tests.unit.test_media_client import FakeResponseReader, FakeResponseWriter, public_resolver


@pytest.mark.parametrize(
    ("mime", "media_class", "prefix", "allowed"),
    [
        ("image/jpeg", "image", b"\xff\xd8\xff\xe0rest", True),
        ("image/png", "image", b"\x89PNG\r\n\x1a\nrest", True),
        ("image/gif", "image", b"GIF89arest", True),
        ("image/webp", "image", b"RIFF\x10\x00\x00\x00WEBP", True),
        ("video/mp4", "video", b"\x00\x00\x00\x18ftypmp42", True),
        ("video/webm", "video", b"\x1a\x45\xdf\xa3rest", True),
        ("image/svg+xml", "image", b"<svg/>", False),
        ("text/html", "image", b"<html>", False),
        ("image/jpeg", "video", b"\xff\xd8\xff", False),
        ("video/mp4", "image", b"\x00\x00\x00\x18ftypmp42", False),
        ("image/webp", "image", b"RIFF\x10\x00\x00\x00WAVE", False),
        ("video/mp4", "video", b"\x00\x00\x00\x04ftypmp42", False),
        ("video/mp4", "video", b"\x00\x00\x00\x18mdatmp42", False),
        ("video/webm", "video", b"<html>", False),
    ],
)
def test_view_signature_policy(mime: str, media_class: str, prefix: bytes, allowed: bool) -> None:
    """被動格式必須符合 token 類型與來源檔頭，不只相信 MIME。"""
    assert validate_view_signature(mime, media_class, prefix) is allowed


@pytest.mark.asyncio
async def test_view_headers_only_project_valid_metadata() -> None:
    """Inline 回應不透傳來源 headers，並保留 CSP 與安全檔名。"""
    upstream = response(
        206,
        {
            "content-type": "video/mp4",
            "content-range": "bytes 2-5/10",
            "content-length": "4",
            "set-cookie": "secret",
            "etag": "secret",
            "location": "https://secret.example/a",
        },
    )
    metadata = validate_view_response(upstream, parse_view_range(["bytes=2-5"]), max_bytes=100)
    headers = build_view_headers("../bad\r\nfilename.mp4", "video/mp4", metadata)
    assert headers["Content-Disposition"].startswith("inline;")
    assert "\r" not in headers["Content-Disposition"]
    assert "\n" not in headers["Content-Disposition"]
    assert headers["Content-Range"] == "bytes 2-5/10"
    assert headers["Content-Length"] == "4"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert "secret" not in str(headers)
    assert "Set-Cookie" not in headers


def response(status: int, headers: dict[str, str]) -> MediaResponse:
    """建立執行正式 header 正規化的記憶體 response。"""
    return MediaResponse(
        status, headers, asyncio.StreamReader(), FakeResponseWriter(), max_bytes=100
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "content_range", "length"),
    [
        ("bytes=2-5", "bytes 2-5/10", 4),
        ("bytes=4-", "bytes 4-9/10", 6),
        ("bytes=-3", "bytes 7-9/10", 3),
        ("bytes=2-99", "bytes 2-9/10", 8),
        ("bytes=-20", "bytes 0-9/10", 10),
    ],
)
async def test_view_partial_matches_request(
    requested: str, content_range: str, length: int
) -> None:
    """三種範圍以 total 計算精確區間，允許尾端 clamp。"""
    result = validate_view_response(
        response(206, {"content-range": content_range, "content-length": str(length)}),
        parse_view_range([requested]),
        max_bytes=100,
    )
    assert result.length == length
    assert result.total == 10
    assert result.content_range == content_range


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "requested", "limit"),
    [
        (200, {"content-length": "10"}, "bytes=2-5", 100),
        (206, {"content-range": "bytes 3-6/10"}, "bytes=2-5", 100),
        (206, {"content-range": "bytes 2-5/*"}, "bytes=2-5", 100),
        (206, {"content-range": "bytes 2-5/10", "content-length": "3"}, "bytes=2-5", 100),
        (206, {"content-range": "bytes 2-5/1000"}, "bytes=2-5", 100),
        (206, {"content-range": "bytes 0-1/10"}, None, 100),
        (200, {"content-range": "bytes 0-9/10"}, None, 100),
        (200, {"content-length": "101"}, None, 100),
    ],
)
async def test_view_rejects_wrong_response(
    status: int,
    headers: dict[str, str],
    requested: str | None,
    limit: int,
) -> None:
    """來源忽略 Range、錯位、未知或過大 total 與長度衝突皆拒絕。"""
    with pytest.raises(AppError) as error:
        validate_view_response(
            response(status, headers),
            parse_view_range([requested] if requested else []),
            max_bytes=limit,
        )
    assert error.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("header, total", [("bytes */10", 10), ("garbage", None), ("", None)])
async def test_view_416_only_exposes_valid_total(header: str, total: int | None) -> None:
    """416 不包含來源 body，僅攜帶已驗證且有界的 total。"""
    with pytest.raises(AppError) as error:
        validate_view_response(
            response(416, {"content-range": header}),
            parse_view_range(["bytes=20-"]),
            max_bytes=100,
        )
    assert error.value.code == "view_range_not_satisfiable"
    assert getattr(error.value, "total", None) == total


@pytest.mark.asyncio
async def test_view_complete_response() -> None:
    """無 Range 只接受完整 200，保留可信完整長度。"""
    result = validate_view_response(response(200, {"content-length": "10"}), None, max_bytes=100)
    assert result.length == 10
    assert result.content_range is None


@pytest.mark.asyncio
async def test_view_range_cannot_be_combined_with_resume() -> None:
    """檢視範圍不可與 attachment offset 混用且不得啟動連線。"""
    client = MediaClient(
        MediaDestinationPolicy(
            allowed_exact_hosts=frozenset({"video.twimg.com"}),
            allowed_suffixes=frozenset(),
            resolver=public_resolver,
        )
    )
    with pytest.raises(AppError) as error:
        await client.fetch(
            "https://video.twimg.com/a.mp4",
            range_start=1,
            view_range=parse_view_range(["bytes=2-5"]),
        )
    assert error.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_view_range_is_sent_as_canonical_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真正 transport 僅送出由型別值產生的 canonical Range。"""
    writer = FakeResponseWriter()
    reader = asyncio.StreamReader()
    reader.feed_data(b"HTTP/1.1 206 Partial Content\r\nContent-Length: 4\r\n\r\ncdef")
    reader.feed_eof()

    async def connect(*args: object, **kwargs: object) -> tuple[asyncio.StreamReader, object]:
        """以記憶體 HTTP peer 擷取真實 client 序列化的請求。"""
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    client = MediaClient(
        MediaDestinationPolicy(
            allowed_exact_hosts=frozenset({"video.twimg.com"}),
            allowed_suffixes=frozenset(),
            resolver=public_resolver,
        )
    )
    response = await client.fetch(
        "https://video.twimg.com/a.mp4", view_range=parse_view_range(["bytes=002-005"])
    )
    await response.close()
    assert b"Range: bytes=2-5\r\n" in writer.writes[0]
    assert b"bytes=002-005" not in writer.writes[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["Cookie", "Authorization", "If-Range", "Range", "Host", "X-Forwarded-For"]
)
async def test_view_range_rejects_metadata_header(name: str) -> None:
    """檢視 typed Range 不授權 metadata 注入憑證或其他來源控制 header。"""
    client = MediaClient(
        MediaDestinationPolicy(
            allowed_exact_hosts=frozenset({"video.twimg.com"}),
            allowed_suffixes=frozenset(),
            resolver=public_resolver,
        )
    )
    with pytest.raises(AppError) as error:
        await client.fetch(
            "https://video.twimg.com/a.mp4",
            headers={name: "sentinel"},
            view_range=parse_view_range(["bytes=-3"]),
        )
    assert error.value.code == "extraction_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", [False, True])
async def test_view_range_redirect_is_revalidated(
    monkeypatch: pytest.MonkeyPatch,
    unsafe: bool,
) -> None:
    """跨 CDN redirect 重新驗證目的地，未核准網域不得收到請求。"""
    location = "https://evil.example/a.mp4" if unsafe else "https://pbs.twimg.com/a.mp4"
    readers = [
        FakeResponseReader(f"HTTP/1.1 302 Found\r\nLocation: {location}\r\n\r\n".encode()),
        FakeResponseReader(b"HTTP/1.1 206 Partial Content\r\nContent-Length: 3\r\n\r\n", b"hij"),
    ]
    writers: list[FakeResponseWriter] = []

    async def connect(*args: object, **kwargs: object) -> tuple[object, object]:
        """只替換 socket peer，保留真正 DNS 政策與 request header 組裝。"""
        writer = FakeResponseWriter()
        writers.append(writer)
        return readers.pop(0), writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    client = MediaClient(
        MediaDestinationPolicy(
            allowed_exact_hosts=frozenset({"video.twimg.com", "pbs.twimg.com"}),
            allowed_suffixes=frozenset(),
            resolver=public_resolver,
        )
    )
    if unsafe:
        with pytest.raises(AppError) as error:
            await client.fetch(
                "https://video.twimg.com/a.mp4", view_range=parse_view_range(["bytes=-3"])
            )
        assert error.value.code == "unsafe_destination"
        assert len(writers) == 1
    else:
        result = await client.fetch(
            "https://video.twimg.com/a.mp4", view_range=parse_view_range(["bytes=-3"])
        )
        assert result.status_code == 206
        await result.close()
        assert len(writers) == 2
        assert b"Host: pbs.twimg.com\r\n" in writers[1].writes[0]
    assert all(b"Range: bytes=-3\r\n" in writer.writes[0] for writer in writers)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([], None),
        (["bytes=2-5"], "bytes=2-5"),
        (["bytes=4-"], "bytes=4-"),
        (["bytes=-3"], "bytes=-3"),
        (["bytes=002-005"], "bytes=2-5"),
    ],
)
def test_view_range_canonical_header(headers: list[str], expected: str | None) -> None:
    """合法單一範圍產生 canonical header，不直接沿用輸入文字。"""
    result = parse_view_range(headers)
    assert (result.header if result else None) == expected


@pytest.mark.parametrize(
    "headers",
    [
        ["bytes=8-2"],
        ["bytes=-0"],
        ["bytes=0-1,4-5"],
        ["bytes=0-1", "bytes=4-5"],
        ["items=0-1"],
        ["bytes=０-１"],
        ["bytes=+1-2"],
        ["bytes=1-2\r\n"],
        ["bytes=-"],
        ["bytes=" + "1" * 123 + "-"],
        [""],
    ],
)
def test_view_range_rejects_invalid_headers(headers: list[str]) -> None:
    """非法、多段、重複或無界範圍以固定錯誤拒絕。"""
    with pytest.raises(AppError) as error:
        parse_view_range(headers)
    assert error.value.code == "view_range_not_satisfiable"
    assert error.value.status_code == 416
