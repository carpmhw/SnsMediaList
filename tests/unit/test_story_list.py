"""驗證帳號 Story 清單的逐則身分、候選與安全邊界。"""

from copy import deepcopy

import pytest

from sns_media_list.errors import AppError
from sns_media_list.extractor.gallery_dl import _add_post_context
from sns_media_list.extractor.normalizer import normalize_gallery_output
from sns_media_list.url_validation import validate_post_url

LIST_URL = "https://www.instagram.com/stories/example.user/"


def story_record(media_id: object, **overrides: object) -> dict[str, object]:
    """建立具有 reel 身分及同筆媒體 ID 的合成 URL event metadata。"""
    return {
        "type": "story",
        "post_id": "999",
        "media_id": media_id,
        "num": 1,
        "url": "https://cdn.example/image.jpg",
        "extension": "jpg",
        "width": 640,
        "height": 480,
        **overrides,
    }


def test_story_list_preserves_distinct_ids_order_and_input() -> None:
    """不同 Story 同 num 不得合併，相同 ID 畫質候選維持首次順序。"""
    raw = [story_record("202"), story_record(101), story_record("202", width=1280)]
    original = deepcopy(raw)
    records = _add_post_context(raw, validate_post_url(LIST_URL))
    assert raw == original
    assert [record["post_id"] for record in records] == ["202", "101", "202"]
    assert [record["num"] for record in records] == [1, 2, 1]
    assert records[0]["post_url"] == LIST_URL + "202/"
    result = normalize_gallery_output(records)
    assert [item.filename for item in result.items] == [
        "instagram-202-1.jpg",
        "instagram-101-1.jpg",
    ]
    assert result.items[0].width == 1280


@pytest.mark.parametrize("media_id", [None, True, False, "", "abc", "1_2", "１２", -1, 1.5])
def test_story_list_rejects_invalid_same_record_identity(media_id: object) -> None:
    """非法逐則 ID 不可借用 reel ID 或其他 record。"""
    with pytest.raises(AppError) as caught:
        _add_post_context(
            [story_record("101"), story_record(media_id)], validate_post_url(LIST_URL)
        )
    assert caught.value.code == "extraction_failed"


def test_story_list_rejects_missing_identity() -> None:
    """缺少 ID 時不使用 post_id 代替媒體身分。"""
    record = story_record("101")
    del record["media_id"]
    with pytest.raises(AppError) as caught:
        _add_post_context([record], validate_post_url(LIST_URL))
    assert caught.value.code == "extraction_failed"


def test_story_list_converts_only_same_record_video_source() -> None:
    """清單影片僅使用同筆來源，直接圖片與影片 URL 不被改寫。"""
    records = _add_post_context(
        [
            story_record("101"),
            story_record(
                "202",
                url="ytdl:synthetic",
                video_url="https://cdn.example/video.mp4",
                extension="mp4",
            ),
            story_record(
                "303",
                url="https://cdn.example/direct.mp4",
                video_url="https://cdn.example/other.mp4",
                extension="mp4",
            ),
        ],
        validate_post_url(LIST_URL),
    )
    assert [record["url"] for record in records] == [
        "https://cdn.example/image.jpg",
        "https://cdn.example/video.mp4",
        "https://cdn.example/direct.mp4",
    ]
    result = normalize_gallery_output(records)
    assert [item.media_type for item in result.items] == ["image", "video", "video"]
    assert result.items[1].filename == "instagram-202-1.mp4"


@pytest.mark.parametrize("video_url", [None, "", 123, [], "http://cdn.example/video.mp4"])
def test_story_list_does_not_borrow_or_repair_video_source(video_url: object) -> None:
    """缺少或非法同筆來源不會借用相鄰來源或轉成安全 URL。"""
    records = _add_post_context(
        [
            story_record("101", video_url="https://cdn.example/good.mp4"),
            story_record(
                "202",
                url="ytdl:synthetic",
                video_url=video_url,
                metadata={"video_url": "https://cdn.example/nested.mp4"},
            ),
        ],
        validate_post_url(LIST_URL),
    )
    with pytest.raises(AppError) as caught:
        normalize_gallery_output(records)
    assert caught.value.code == "extraction_failed"


@pytest.mark.parametrize("has_available_variant", [False, True])
def test_story_list_unavailable_counts_distinct_stories_without_any_valid_variant(
    has_available_variant: bool,
) -> None:
    """只計完全沒有可下載候選的 Story，不計重複或已有可用版本者。"""
    raw = [
        story_record("101", progressive=False),
        story_record("101", progressive=False),
        story_record("202"),
    ]
    if has_available_variant:
        raw.append(story_record("101"))
    target = validate_post_url(LIST_URL)
    result = normalize_gallery_output(_add_post_context(raw, target), target=target)
    assert result.unavailable_media_count == (0 if has_available_variant else 1)
    assert len(result.items) == (2 if has_available_variant else 1)
