"""Application error types and HTTP status mapping."""

from dataclasses import dataclass, field
from typing import Literal, cast

type FailureStage = Literal[
    "extractor_start",
    "extractor_timeout",
    "extractor_output_limit",
    "extractor_io",
    "extractor_process_unclassified",
    "extractor_invalid_output",
    "extractor_empty_output",
    "extractor_no_media",
    "extractor_platform_error",
]
type FailureStageValue = FailureStage | Literal["unknown"]
type ExtractorDiagnosticSource = Literal["stderr", "datajob_error", "unknown"]
type ExtractorErrorType = Literal[
    "auth_required",
    "authentication_error",
    "authorization_error",
    "not_found",
    "http_error",
    "challenge_error",
    "extraction_error",
    "no_extractor",
    "unknown",
]

FAILURE_STAGES = frozenset(
    {
        "extractor_start",
        "extractor_timeout",
        "extractor_output_limit",
        "extractor_io",
        "extractor_process_unclassified",
        "extractor_invalid_output",
        "extractor_empty_output",
        "extractor_no_media",
        "extractor_platform_error",
        "unknown",
    }
)


def normalize_failure_stage(value: object) -> FailureStageValue | None:
    """將內部 failure stage 限制為固定白名單值。"""
    if value is None:
        return None
    if type(value) is str and value in FAILURE_STAGES:
        return cast(FailureStageValue, value)
    return "unknown"


_EXTRACTOR_DIAGNOSTIC_SOURCES = frozenset({"stderr", "datajob_error", "unknown"})
_EXTRACTOR_ERROR_TYPES = frozenset(
    {
        "auth_required",
        "authentication_error",
        "authorization_error",
        "not_found",
        "http_error",
        "challenge_error",
        "extraction_error",
        "no_extractor",
        "unknown",
    }
)


def _normalize_extractor_enum(value: object, allowed: frozenset[str]) -> str | None:
    """將 extractor enum 限制為固定字串，避免轉換任意輸入。"""
    if value is None:
        return None
    if type(value) is str and value in allowed:
        return value
    return "unknown"


def _normalize_extractor_exit_code(value: object) -> int | None:
    """保留範圍內的精確整數退出碼，不執行隱式轉型。"""
    if type(value) is int and -255 <= value <= 255:
        return value
    return None


def _normalize_extractor_http_statuses(value: object) -> tuple[int, ...] | None:
    """將內建短 list／tuple 狀態集合正規化為排序後的 tuple。"""
    statuses: list[object] | tuple[object, ...]
    if type(value) is list:
        statuses = cast(list[object], value)
    elif type(value) is tuple:
        statuses = cast(tuple[object, ...], value)
    else:
        return None

    if not statuses or len(statuses) > 8:
        return None
    if any(type(status) is not int or not 100 <= status <= 599 for status in statuses):
        return None

    return tuple(sorted(set(cast(int, status) for status in statuses)))


@dataclass(frozen=True, slots=True, init=False)
class ExtractorDiagnostics:
    """保存經白名單與界限正規化的 extractor 診斷 metadata。"""

    extractor_diagnostic_source: ExtractorDiagnosticSource | None = None
    extractor_error_type: ExtractorErrorType | None = None
    extractor_exit_code: int | None = None
    extractor_http_statuses: tuple[int, ...] | None = None

    def __init__(
        self,
        extractor_diagnostic_source: object = None,
        extractor_error_type: object = None,
        extractor_exit_code: object = None,
        extractor_http_statuses: object = None,
    ) -> None:
        """建立僅含固定 enum、界限整數與有界狀態的不可變 metadata。"""
        source = _normalize_extractor_enum(
            extractor_diagnostic_source,
            _EXTRACTOR_DIAGNOSTIC_SOURCES,
        )
        error_type = _normalize_extractor_enum(extractor_error_type, _EXTRACTOR_ERROR_TYPES)
        object.__setattr__(
            self,
            "extractor_diagnostic_source",
            cast(ExtractorDiagnosticSource | None, source),
        )
        object.__setattr__(
            self,
            "extractor_error_type",
            cast(ExtractorErrorType | None, error_type),
        )
        object.__setattr__(
            self,
            "extractor_exit_code",
            _normalize_extractor_exit_code(extractor_exit_code),
        )
        object.__setattr__(
            self,
            "extractor_http_statuses",
            _normalize_extractor_http_statuses(extractor_http_statuses),
        )


def normalize_extractor_diagnostics(
    *,
    extractor_diagnostic_source: object = None,
    extractor_error_type: object = None,
    extractor_exit_code: object = None,
    extractor_http_statuses: object = None,
) -> ExtractorDiagnostics:
    """以共用安全規則建立 extractor 診斷 metadata。"""
    return ExtractorDiagnostics(
        extractor_diagnostic_source=extractor_diagnostic_source,
        extractor_error_type=extractor_error_type,
        extractor_exit_code=extractor_exit_code,
        extractor_http_statuses=extractor_http_statuses,
    )


ERROR_STATUS: dict[str, int] = {
    "invalid_request": 422,
    "invalid_url": 400,
    "unsupported_url": 400,
    "post_unavailable": 404,
    "story_unavailable": 404,
    "story_auth_required": 403,
    "token_not_found": 404,
    "no_media": 422,
    "extraction_limit_exceeded": 422,
    "request_too_large": 413,
    "unsupported_media_type": 415,
    "local_rate_limited": 429,
    "upstream_rate_limited": 429,
    "upstream_media_invalid": 502,
    "extraction_failed": 502,
    "capacity_exceeded": 503,
    "platform_authentication_failed": 503,
    "extraction_timeout": 504,
    "token_expired": 410,
    "unsafe_destination": 502,
}


@dataclass(eq=False)
class AppError(Exception):
    """Represent a safe, stable error returned by the application."""

    code: str
    message: str
    status_code: int | None = None
    retry_after: int | None = None
    deterministic: bool = False
    failure_stage: FailureStageValue | None = field(default=None, kw_only=True)
    extractor_diagnostics: ExtractorDiagnostics | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """補上預設 HTTP 狀態並正規化內部診斷 metadata。"""
        super().__init__(self.message)
        if self.status_code is None:
            self.status_code = ERROR_STATUS.get(self.code, 500)
        self.failure_stage = normalize_failure_stage(self.failure_stage)
        if self.extractor_diagnostics is not None:
            if type(self.extractor_diagnostics) is not ExtractorDiagnostics:
                self.extractor_diagnostics = None
            else:
                self.extractor_diagnostics = normalize_extractor_diagnostics(
                    extractor_diagnostic_source=(
                        self.extractor_diagnostics.extractor_diagnostic_source
                    ),
                    extractor_error_type=self.extractor_diagnostics.extractor_error_type,
                    extractor_exit_code=self.extractor_diagnostics.extractor_exit_code,
                    extractor_http_statuses=self.extractor_diagnostics.extractor_http_statuses,
                )
