"""Browser-facing static contract tests for the contact-sheet workflow."""

from fastapi.testclient import TestClient

from sns_media_list.app import create_app


def test_home_page_contains_contact_sheet_workflow() -> None:
    """Verify the served page contains the accessible submission structure."""
    response = TestClient(create_app()).get("/")

    assert response.status_code == 200
    assert '<html lang="zh-Hant">' in response.text
    assert "將一則公開貼文整理成清晰的下載清單。" in response.text
    assert "分析公開貼文" in response.text
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
    assert "平台暫時限制匿名存取" in javascript.text
    assert "下載參照已過期" in javascript.text
    assert "重新分析" in javascript.text
    assert "/api/extractions" in javascript.text
    assert "token_expired" in javascript.text
    assert "token_not_found" in javascript.text


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
    assert "平台暫時限制匿名存取" in response.text
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
