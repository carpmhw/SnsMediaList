"""Browser-facing static contract tests for the contact-sheet workflow."""

import struct
import zlib

from fastapi.testclient import TestClient

from sns_media_list.app import create_app


def _png_corner_rgba(payload: bytes) -> tuple[int, int, int, int]:
    """Read the transparent top-left pixel from an RGBA PNG payload."""
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    chunk_length, chunk_type = struct.unpack_from(">I4s", payload, 8)
    assert chunk_type == b"IHDR"
    ihdr = payload[16 : 16 + chunk_length]
    bit_depth, color_type, interlace = struct.unpack_from(">BBB", ihdr, 8)
    assert (bit_depth, color_type, interlace) == (8, 6, 0)

    compressed = bytearray()
    offset = 8
    while offset < len(payload):
        length, chunk_type = struct.unpack_from(">I4s", payload, offset)
        chunk_start = offset + 8
        if chunk_type == b"IDAT":
            compressed.extend(payload[chunk_start : chunk_start + length])
        offset = chunk_start + length + 4
        if chunk_type == b"IEND":
            break

    pixels = zlib.decompress(compressed)
    return pixels[1], pixels[2], pixels[3], pixels[4]


def test_home_page_contains_contact_sheet_workflow() -> None:
    """Verify the served page contains the accessible submission structure."""
    response = TestClient(create_app()).get("/")

    assert response.status_code == 200
    assert '<html lang="zh-Hant">' in response.text
    assert "將一則內容整理成清晰的下載清單。" in response.text
    assert "分析單則內容" in response.text
    assert "Instagram 貼文、Reel、單則 Story 與 X 狀態貼文" in response.text
    assert "帳號目前全部 Stories 與 Highlights 不支援" in response.text
    assert "可信部署若使用服務管理者的 Instagram 工作階段" in response.text
    assert "任何服務使用者都可能間接存取該帳號可見的私人或受眾限定 Story" in response.text
    assert "X status" not in response.text
    assert "operator session" not in response.text
    assert "請只下載你有權保存的內容" in response.text
    assert 'id="extraction-form"' in response.text
    assert 'id="post-url"' in response.text
    assert '<form id="extraction-form">' in response.text
    assert '<label for="post-url">' in response.text
    assert 'aria-describedby="url-help"' in response.text
    assert 'aria-live="polite"' in response.text
    assert 'id="analyze-button"' in response.text
    assert 'id="results"' in response.text
    assert 'id="media-grid"' in response.text
    assert 'id="privacy-reminder"' in response.text
    assert "只下載你有權保存的內容" in response.text


def test_home_page_advertises_same_origin_favicon_formats() -> None:
    """Verify the document head advertises both favicon candidates locally."""
    response = TestClient(create_app()).get("/")

    assert response.status_code == 200
    assert (
        '<link rel="icon" href="/favicon.ico" type="image/x-icon" sizes="16x16 32x32 48x48">'
        in response.text
    )
    assert '<link rel="icon" href="/favicon.svg" type="image/svg+xml">' in response.text


def test_svg_favicon_is_self_contained_and_uses_approved_palette() -> None:
    """Verify the SVG favicon is a safe, opaque rendering of the approved design."""
    response = TestClient(create_app()).get("/favicon.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert 'viewBox="0 0 64 64"' in response.text
    for color in ("#171D1C", "#202A27", "#EDB37C", "#F3EEE6", "#3B4741"):
        assert color in response.text
    assert '<rect width="64" height="64" rx="15" fill="#171D1C"' in response.text
    assert "<script" not in response.text
    assert "<text" not in response.text
    assert "<image" not in response.text
    assert "<animate" not in response.text
    assert 'href="http:' not in response.text
    assert 'href="https:' not in response.text
    assert "xlink:href" not in response.text
    assert "url(" not in response.text


def test_ico_favicon_contains_standard_tab_sizes() -> None:
    """Verify the ICO fallback is valid and includes 16, 32, and 48 pixel frames."""
    response = TestClient(create_app()).get("/favicon.ico")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/vnd.microsoft.icon")
    reserved, image_type, image_count = struct.unpack_from("<HHH", response.content)
    assert reserved == 0
    assert image_type == 1
    assert image_count == 3

    directory_end = 6 + image_count * 16
    directory_entries = list(struct.iter_unpack("<BBBBHHII", response.content[6:directory_end]))
    frame_sizes = {
        (width or 256, height or 256)
        for width, height, _colors, _reserved, _planes, _bits, _length, _offset in directory_entries
    }
    assert frame_sizes == {(16, 16), (32, 32), (48, 48)}
    for _width, _height, _colors, _reserved, _planes, _bits, length, offset in directory_entries:
        payload = response.content[offset : offset + length]
        assert _png_corner_rgba(payload)[3] == 0


def test_static_assets_include_responsive_and_recovery_hooks() -> None:
    """Verify CSS and JavaScript expose required responsive behavior hooks."""
    client = TestClient(create_app())
    css = client.get("/styles.css")
    javascript = client.get("/app.js")

    assert css.status_code == 200
    assert "@media" in css.text
    assert ":focus-visible" in css.text
    assert "prefers-reduced-motion" in css.text
    assert javascript.status_code == 200
    assert "平台暫時限制存取" in javascript.text
    assert "下載參照已過期" in javascript.text
    assert "重新分析" in javascript.text
    assert "/api/extractions" in javascript.text
    assert "token_expired" in javascript.text
    assert "token_not_found" in javascript.text
    assert "method: 'HEAD'" in javascript.text
    assert ".blob()" not in javascript.text


def test_javascript_coordinates_token_previews_without_buffering_media() -> None:
    """Verify static client code keeps preview work bounded and cancellable."""
    javascript = TestClient(create_app()).get("/app.js")

    assert javascript.status_code == 200
    assert "previewQueue" in javascript.text
    assert "activePreview" in javascript.text
    assert "extractionGeneration" in javascript.text
    assert "pumpPreviewQueue" in javascript.text
    assert "cancelPreviewLoading" in javascript.text
    assert "PREVIEW_RETRY_DELAY_MS = 1000" in javascript.text
    assert "removeAttribute('src')" in javascript.text
    assert ".blob()" not in javascript.text


def test_local_preview_placeholder_is_served_same_origin() -> None:
    """Verify the fail-closed preview placeholder is a local static asset."""
    response = TestClient(create_app()).get("/placeholder.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "Preview unavailable" in response.text


def test_stylesheet_has_grid_mobile_focus_and_motion_rules() -> None:
    """Verify the stylesheet contains the approved responsive behavior."""
    response = TestClient(create_app()).get("/styles.css")

    assert response.status_code == 200
    assert "grid-template-columns: repeat(auto-fit, minmax(" in response.text
    assert "@media (max-width:" in response.text
    assert ".media-grid" in response.text
    assert ":focus-visible" in response.text
    assert "prefers-reduced-motion" in response.text
    assert "font-size: clamp(1.8rem, 5vw, 3.6rem);" in response.text


def test_javascript_has_same_origin_extraction_and_recovery_hooks() -> None:
    """Verify client code exposes the API workflow and stable error states."""
    response = TestClient(create_app()).get("/app.js")

    assert response.status_code == 200
    assert "fetch('/api/extractions'" in response.text
    assert "token_expired" in response.text
    assert "token_not_found" in response.text
    assert "平台暫時限制存取" in response.text
    assert "story_unavailable" in response.text
    assert "此 Story 目前無法使用。" in response.text
    assert "X 狀態貼文" in response.text
    assert "帳號目前全部 Stories 與 Highlights 不支援" in response.text
    assert "X status" not in response.text
    assert "X STATUS" not in response.text
    assert "正在分析內容..." in response.text
    assert "目前無法分析此內容。" in response.text
    assert "重新分析" in response.text
    assert "local_rate_limited" in response.text
    assert "innerHTML" not in response.text
    assert "pbs.twimg.com" not in response.text
    assert "cdninstagram.com" not in response.text


def test_static_page_uses_no_external_script_or_media_origin() -> None:
    """Verify the browser shell does not load remote executable assets."""
    response = TestClient(create_app()).get("/")

    assert "http://" not in response.text
    assert "https://" not in response.text
