"""驗證已安裝 gallery-dl 的版本、CLI 與 DataJob producer contract。"""

import importlib.metadata
import io
import json
import re
import shutil
import subprocess
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import pytest
from gallery_dl import job as gallery_job
from gallery_dl.extractor.common import Extractor

from scripts import verify_gallery_contract
from sns_media_list.errors import AppError
from sns_media_list.extractor.gallery_dl import (
    _add_post_context,
    _map_json_error_records,
    _parse_json_records,
)
from sns_media_list.extractor.normalizer import normalize_gallery_output
from sns_media_list.url_validation import validate_post_url


class ContractExtractor(Extractor):
    """以固定合成事件驗證實際 gallery-dl DataJob 的輸出格式。"""

    pattern = r"^contract://(?:media|story)$"
    category = "contract"
    subcategory = "fixture"
    root = "https://contract.invalid"

    def __init__(self, match: re.Match[str]) -> None:
        """初始化不含網路或帳號狀態的合成 extractor。"""
        super().__init__(match)
        self.story_mode = match.group(0) == "contract://story"
        self.emit_error = not self.story_mode

    def items(self) -> Iterator[tuple[int, str, dict[str, object]]]:
        """產生 directory、media、queue 與受控 error event。"""
        yield gallery_job.Message.Directory, "", {"title": "synthetic post"}
        if self.story_mode:
            yield (
                gallery_job.Message.Url,
                "ytdl:contract-story-video",
                {
                    "type": "video",
                    "num": 1,
                    "filename": "contract-story-video",
                    "extension": "mp4",
                    "video_url": "https://cdn.example/story-video.mp4",
                    "progressive": True,
                },
            )
        else:
            yield (
                gallery_job.Message.Url,
                "https://cdn.example/image.jpg",
                {
                    "type": "image",
                    "num": 1,
                    "filename": "synthetic-image",
                    "extension": "jpg",
                    "width": 640,
                    "height": 480,
                },
            )
        yield gallery_job.Message.Queue, "contract://unhandled", {"title": "synthetic queue"}
        if self.emit_error:
            raise ValueError("synthetic offline error")


def _project_gallery_dl_pin() -> str:
    """從 pyproject.toml 讀取 gallery-dl 的精確版本 pin。"""
    project_file = Path(__file__).resolve().parents[2] / "pyproject.toml"
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    pins = [
        dependency.removeprefix("gallery-dl==")
        for dependency in dependencies
        if isinstance(dependency, str) and dependency.startswith("gallery-dl==")
    ]
    assert len(pins) == 1
    return pins[0]


def _data_job_output(
    monkeypatch: pytest.MonkeyPatch, extractor_url: str = "contract://media"
) -> bytes:
    """以受控 extractor 執行已安裝套件並擷取單一 JSON event array。"""
    from gallery_dl.extractor import common as gallery_common

    def deny_network_request(
        _session: object,
        method: str,
        url: str,
        **_kwargs: object,
    ) -> NoReturn:
        """若 DataJob 嘗試送出 HTTP 請求，立即令 contract 測試失敗。"""
        raise AssertionError(f"DataJob attempted an unexpected {method} request to {url}")

    monkeypatch.setattr(gallery_common.requests.Session, "request", deny_network_request)
    extractor = ContractExtractor.from_url(extractor_url)
    assert extractor is not None
    output = io.StringIO()
    data_job = gallery_job.DataJob(extractor, file=output, resolve=True)

    assert data_job.run() == 0
    return output.getvalue().encode("utf-8")


def test_contract_runner_includes_installed_package_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """確認正式 contract 入口會執行本檔的安裝套件驗證。"""
    commands: list[list[str]] = []

    def capture_command(
        command: list[str], *, check: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        """攔截 contract runner 子程序命令，避免重複執行測試。"""
        assert not check
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(verify_gallery_contract.subprocess, "run", capture_command)

    assert verify_gallery_contract.main() == 0
    assert len(commands) == 1
    assert "tests/unit/test_gallery_dl_contract.py" in commands[0]


def test_installed_distribution_matches_exact_project_pin() -> None:
    """確認實際安裝 distribution 版本與專案精確 pin 相同。"""
    assert importlib.metadata.version("gallery-dl") == _project_gallery_dl_pin()


def test_installed_cli_accepts_resolve_json() -> None:
    """確認安裝的 gallery-dl CLI 接受 adapter 使用的 --resolve-json。"""
    executable = shutil.which("gallery-dl")
    assert executable is not None

    result = subprocess.run(
        [executable, "--resolve-json", "--version"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert importlib.metadata.version("gallery-dl") in result.stdout


def test_installed_data_job_events_round_trip_through_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """確認 DataJob events、媒體 metadata 與 adapter normalizer 相容。"""
    output = _data_job_output(monkeypatch)
    events = json.loads(output)
    assert isinstance(events, list)
    assert [event[0] for event in events] == [2, 3, 6, -1]

    parsed = _parse_json_records(output)
    assert parsed.literal_empty is False
    assert parsed.saw_non_media is True
    assert len(parsed.media_records) == 1
    assert len(parsed.error_records) == 1
    assert parsed.error_records[0] == {
        "error": "ValueError",
        "message": "synthetic offline error",
    }

    target = validate_post_url("https://www.instagram.com/p/ABC123/")
    records = _add_post_context(list(parsed.media_records), target)
    normalized = normalize_gallery_output(records)
    assert normalized.platform == "instagram"
    assert normalized.post_id == "ABC123"
    assert len(normalized.items) == 1
    assert normalized.items[0].source_url == "https://cdn.example/image.jpg"

    with pytest.raises(AppError) as exc_info:
        _map_json_error_records(
            list(parsed.error_records),
            target_kind=target.kind,
            authenticated=False,
        )
    assert exc_info.value.code == "extraction_failed"


def test_installed_story_pseudo_url_round_trips_through_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """確認已安裝 DataJob 的 Story pseudo URL 可由同筆 video_url 正規化。"""
    output = _data_job_output(monkeypatch, "contract://story")
    events = json.loads(output)
    assert isinstance(events, list)
    assert [event[0] for event in events] == [2, 3, 6]
    assert events[1][1] == "ytdl:contract-story-video"
    assert events[1][2]["video_url"] == "https://cdn.example/story-video.mp4"
    assert "width" not in events[1][2]

    parsed = _parse_json_records(output)
    target = validate_post_url("https://www.instagram.com/stories/contract.user/123456789/")
    records = _add_post_context(list(parsed.media_records), target)
    normalized = normalize_gallery_output(records)

    assert normalized.platform == "instagram"
    assert normalized.post_id == "123456789"
    assert len(normalized.items) == 1
    assert normalized.items[0].media_type == "video"
    assert normalized.items[0].source_url == "https://cdn.example/story-video.mp4"
    assert normalized.items[0].filename == "instagram-123456789-1.mp4"
