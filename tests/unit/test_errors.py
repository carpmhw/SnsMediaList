"""Application error 與內部 diagnostic stage 契約測試。"""

import pytest

from sns_media_list.errors import AppError

_FAILURE_STAGES = (
    "extractor_start",
    "extractor_timeout",
    "extractor_output_limit",
    "extractor_io",
    "extractor_process_unclassified",
    "extractor_invalid_output",
    "extractor_empty_output",
    "extractor_no_media",
    "extractor_platform_error",
)


def test_existing_app_error_constructor_preserves_error_semantics() -> None:
    """驗證新增 stage 前的 AppError 建構語意維持不變。"""
    error = AppError("story_auth_required", "safe message", None, 7, True)

    assert error.code == "story_auth_required"
    assert error.message == "safe message"
    assert error.status_code == 403
    assert error.retry_after == 7
    assert error.deterministic is True
    assert error.failure_stage is None


@pytest.mark.parametrize("failure_stage", (*_FAILURE_STAGES, "unknown"))
def test_app_error_preserves_fixed_failure_stages(failure_stage: str) -> None:
    """驗證 AppError 保留固定且允許的 failure stage。"""
    error = AppError("extraction_failed", "safe message", failure_stage=failure_stage)

    assert error.failure_stage == failure_stage


@pytest.mark.parametrize(
    "failure_stage",
    [
        pytest.param("https://example.test/?token=PRIVATE_TOKEN", id="url"),
        pytest.param("../../secret", id="path"),
        pytest.param("sessionid=PRIVATE_COOKIE", id="cookie"),
        pytest.param("", id="empty"),
        pytest.param("x" * 4096, id="too-long"),
        pytest.param(42, id="non-string"),
        pytest.param(object(), id="arbitrary-object"),
    ],
)
def test_app_error_normalizes_untrusted_failure_stages(failure_stage: object) -> None:
    """驗證任意或非字串 stage 只會降為固定 unknown。"""
    error = AppError("extraction_failed", "safe message", failure_stage=failure_stage)

    assert error.failure_stage == "unknown"


def test_app_error_does_not_stringify_untrusted_failure_stage() -> None:
    """驗證 stage 正規化不呼叫任意物件的字串轉換。"""

    class UnprintableStage:
        """在測試中偵測不安全的任意字串轉換。"""

        def __str__(self) -> str:
            """若實作嘗試轉換 stage，立即讓 regression 失敗。"""
            raise AssertionError("failure stage must not be stringified")

    error = AppError(
        "extraction_failed",
        "safe message",
        failure_stage=UnprintableStage(),
    )

    assert error.failure_stage == "unknown"


def test_app_error_does_not_hash_untrusted_string_subclasses() -> None:
    """驗證 stage 正規化不執行字串子類覆寫的雜湊操作。"""

    class UnhashableStage(str):
        """模擬帶有自訂雜湊行為的字串子類。"""

        def __hash__(self) -> int:
            """若白名單查詢呼叫自訂雜湊，立即讓 regression 失敗。"""
            raise AssertionError("failure stage must not invoke custom hashing")

    error = AppError(
        "extraction_failed",
        "safe message",
        failure_stage=UnhashableStage("extractor_io"),
    )

    assert error.failure_stage == "unknown"
