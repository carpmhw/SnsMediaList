"""Application error 與內部 diagnostic stage 契約測試。"""

from collections.abc import Iterator
from dataclasses import FrozenInstanceError

import pytest

from sns_media_list.errors import AppError, ExtractorDiagnostics

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
    assert error.extractor_diagnostics is None


def test_app_error_accepts_optional_keyword_only_extractor_diagnostics() -> None:
    """驗證舊建構參數相容且 AppError 可接收 keyword-only 診斷 metadata。"""
    diagnostics = ExtractorDiagnostics(
        extractor_diagnostic_source="stderr",
        extractor_error_type="unknown",
        extractor_exit_code=0,
        extractor_http_statuses=(403,),
    )
    try:
        error = AppError(
            "extraction_failed",
            "safe message",
            extractor_diagnostics=diagnostics,
        )
    except TypeError as error:
        raise AssertionError("AppError 必須接受 extractor_diagnostics keyword-only 欄位") from error

    assert error.extractor_diagnostics == diagnostics


@pytest.mark.parametrize("source", ["stderr", "datajob_error", "unknown"])
def test_extractor_diagnostics_preserves_fixed_sources(source: str) -> None:
    """驗證診斷來源僅保留固定 enum。"""
    diagnostics = ExtractorDiagnostics(extractor_diagnostic_source=source)

    assert diagnostics.extractor_diagnostic_source == source


@pytest.mark.parametrize(
    "error_type",
    [
        "auth_required",
        "authentication_error",
        "authorization_error",
        "not_found",
        "http_error",
        "challenge_error",
        "extraction_error",
        "no_extractor",
        "unknown",
    ],
)
def test_extractor_diagnostics_preserves_fixed_error_types(error_type: str) -> None:
    """驗證 extractor 錯誤類別僅保留固定 enum。"""
    diagnostics = ExtractorDiagnostics(extractor_error_type=error_type)

    assert diagnostics.extractor_error_type == error_type


@pytest.mark.parametrize("exit_code", [-255, -9, 0, 255])
def test_extractor_diagnostics_preserves_bounded_exit_codes(exit_code: int) -> None:
    """驗證有效程序退出碼包含零與負值並保留原值。"""
    diagnostics = ExtractorDiagnostics(extractor_exit_code=exit_code)

    assert diagnostics.extractor_exit_code == exit_code


def test_extractor_diagnostics_sorts_and_deduplicates_http_statuses() -> None:
    """驗證 HTTP 狀態使用排序、去重後的不可變 tuple。"""
    diagnostics = ExtractorDiagnostics(
        extractor_http_statuses=[403, 401, 403],
    )

    assert diagnostics.extractor_http_statuses == (401, 403)


def test_extractor_diagnostics_accepts_eight_http_statuses_at_range_bounds() -> None:
    """驗證八個狀態及 HTTP 狀態範圍端點均可保留。"""
    diagnostics = ExtractorDiagnostics(
        extractor_http_statuses=[599, 100, 598, 101, 597, 102, 596, 103],
    )

    assert diagnostics.extractor_http_statuses == (
        100,
        101,
        102,
        103,
        596,
        597,
        598,
        599,
    )


@pytest.mark.parametrize("statuses", [None, [], (), [403, 403]])
def test_extractor_diagnostics_omits_empty_http_statuses(
    statuses: list[int] | tuple[int, ...] | None,
) -> None:
    """驗證缺少或空的 HTTP 狀態集合會省略欄位。"""
    diagnostics = ExtractorDiagnostics(extractor_http_statuses=statuses)

    expected = (403,) if statuses == [403, 403] else None
    assert diagnostics.extractor_http_statuses == expected


def test_extractor_diagnostics_is_immutable() -> None:
    """驗證診斷 metadata 建立後不可變更。"""
    diagnostics = ExtractorDiagnostics(extractor_exit_code=0)

    with pytest.raises(FrozenInstanceError):
        diagnostics.extractor_exit_code = 1


def test_extractor_diagnostics_defaults_all_fields_to_none() -> None:
    """驗證未提供的診斷欄位維持省略狀態。"""
    diagnostics = ExtractorDiagnostics()

    assert diagnostics.extractor_diagnostic_source is None
    assert diagnostics.extractor_error_type is None
    assert diagnostics.extractor_exit_code is None
    assert diagnostics.extractor_http_statuses is None


@pytest.mark.parametrize("field_name", ["extractor_diagnostic_source", "extractor_error_type"])
def test_extractor_diagnostics_replaces_untrusted_enum_with_unknown(field_name: str) -> None:
    """驗證惡意 enum 輸入被固定降級且不保留 raw input。"""
    sentinel = "https://example.test/story/PRIVATE_ID/?token=PRIVATE_TOKEN"
    diagnostics = ExtractorDiagnostics(**{field_name: sentinel})

    assert getattr(diagnostics, field_name) == "unknown"
    assert sentinel not in repr(diagnostics)


@pytest.mark.parametrize("exit_code", [True, False, 1.5, "3", -256, 256, 2**100])
def test_extractor_diagnostics_omits_invalid_exit_codes(exit_code: object) -> None:
    """驗證非精確整數及超界退出碼會省略而不轉型。"""
    diagnostics = ExtractorDiagnostics(extractor_exit_code=exit_code)

    assert diagnostics.extractor_exit_code is None


@pytest.mark.parametrize(
    "statuses",
    [
        [99],
        [600],
        [True],
        [403, "429"],
        [*range(100, 109)],
    ],
)
def test_extractor_diagnostics_omits_invalid_http_status_collections(
    statuses: list[object],
) -> None:
    """驗證非法元素、狀態範圍及超長集合都會使整欄省略。"""
    diagnostics = ExtractorDiagnostics(extractor_http_statuses=statuses)

    assert diagnostics.extractor_http_statuses is None


class _UnprintableValue:
    """用於偵測診斷正規化是否呼叫任意物件轉型。"""

    def __str__(self) -> str:
        """若實作嘗試將惡意值轉成字串，立即讓測試失敗。"""
        raise AssertionError("diagnostic input must not be stringified")

    def __int__(self) -> int:
        """若實作嘗試將惡意值轉成整數，立即讓測試失敗。"""
        raise AssertionError("diagnostic input must not be converted to int")


class _NonIterableStatuses:
    """用於偵測正規化是否呼叫自訂狀態 iterator。"""

    def __iter__(self) -> Iterator[int]:
        """若實作遍歷任意 iterable，立即讓測試失敗。"""
        raise AssertionError("diagnostic status input must not be iterated")


class _ListWithCustomIteration(list[int]):
    """用於偵測正規化是否呼叫 list 子類覆寫的迭代器。"""

    def __iter__(self) -> Iterator[int]:
        """若實作遍歷 list 子類，立即讓測試失敗。"""
        raise AssertionError("diagnostic status input must be a built-in list")


def _status_generator(iterations: list[int]) -> Iterator[int]:
    """建立可觀察是否被消耗的狀態 generator。"""
    iterations.append(1)
    yield 403


def test_extractor_diagnostics_does_not_convert_arbitrary_values() -> None:
    """驗證 enum、退出碼與狀態元素不呼叫任意字串或數字轉換。"""
    diagnostics = ExtractorDiagnostics(
        extractor_diagnostic_source=_UnprintableValue(),
        extractor_error_type=_UnprintableValue(),
        extractor_exit_code=_UnprintableValue(),
        extractor_http_statuses=[_UnprintableValue()],
    )

    assert diagnostics.extractor_diagnostic_source == "unknown"
    assert diagnostics.extractor_error_type == "unknown"
    assert diagnostics.extractor_exit_code is None
    assert diagnostics.extractor_http_statuses is None


@pytest.mark.parametrize(
    "statuses",
    [iter([403]), _NonIterableStatuses(), _ListWithCustomIteration([403])],
)
def test_extractor_diagnostics_does_not_iterate_custom_status_inputs(statuses: object) -> None:
    """驗證 generator 與自訂 iterable 均不被消耗。"""
    iterations: list[int] = []
    if isinstance(statuses, Iterator):
        statuses = _status_generator(iterations)
    diagnostics = ExtractorDiagnostics(extractor_http_statuses=statuses)

    assert diagnostics.extractor_http_statuses is None
    assert iterations == []


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
