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

    def __post_init__(self) -> None:
        """補上預設 HTTP 狀態並正規化內部 failure stage。"""
        super().__init__(self.message)
        if self.status_code is None:
            self.status_code = ERROR_STATUS.get(self.code, 500)
        self.failure_stage = normalize_failure_stage(self.failure_stage)
