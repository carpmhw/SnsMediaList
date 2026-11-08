"""以真實 gallery-dl 帳號解析及 DataJob 驅動無外網的 Story 成功流程。"""

import re
import sys
from collections.abc import Iterator

from gallery_dl import job
from gallery_dl.extractor import instagram
from requests import Response, Session


class OfflineStoriesExtractor(instagram.InstagramStoriesExtractor):
    """保留 1.32.19 帳號解析與精確 Story 篩選，僅替換上游及媒體事件。"""

    def config(self, key: str, default: object = None) -> object:
        """讀取正式 subprocess command 中的 Instagram 選項。"""
        prefix = f"extractor.instagram.{key}="
        if key == "user-cache":
            return False
        return next((arg[len(prefix) :] for arg in sys.argv if arg.startswith(prefix)), default)

    def _init(self) -> None:
        """沿用真實初始化，將個人頁、GraphQL 與 Story 清單換成固定資料。"""
        super()._init()
        self.api.reels_media = self.fixture_reels
        self.api._call_graphql = self.fixture_graphql

    def request(self, url: str, **_kwargs: object) -> Response:
        """只回應合成的個人頁，其他請求一律拒絕，測試不得接觸平台。"""
        if url.rstrip("/").endswith("/" + self.user):
            response = Response()
            response.status_code = 200
            response._content = (
                '{"profile_id":"12345","params":{"username":"' + self.user + '"}}'
            ).encode()
            return response
        raise self.exc.NotFoundError("user")

    def fixture_graphql(
        self, opname: str, variables: dict[str, object], **_kwargs: object
    ) -> dict[str, object]:
        """提供 1.32.19 帳號查詢所需的合成 GraphQL 回應。"""
        assert opname == "PolarisProfilePageContentQuery"
        assert variables["id"] == "12345"
        assert variables["enable_integrity_filters"] is True
        return {"user": {"id": "12345", "username": self.user}}

    def fixture_reels(self, reel_ids: tuple[str, ...]) -> Iterator[dict[str, object]]:
        """檢查解析後的身分並提供兩則 Story，交回套件精確 ID 篩選。"""
        assert reel_ids == ("12345",)
        yield {"items": [{"pk": "999999999"}, {"pk": self.media_id}]}

    def items(self) -> Iterator[tuple[int, str, dict[str, object]]]:
        """以套件 posts 篩選的精確 Story 產生真實 DataJob media events。"""
        kind = self.fixture_kind
        for reel in self.posts():
            for item in reel["items"]:
                yield (
                    job.Message.Url,
                    "https://scontent.cdninstagram.com/synthetic-media",
                    {
                        "post_id": item["pk"],
                        "num": 1,
                        "type": kind,
                        "extension": "jpg" if kind == "image" else "mp4",
                        "progressive": True,
                    },
                )


def main(kind: str = "image") -> None:
    """將正式 runner 的 command 接入實際套件 producer，不允許外網。"""
    match = re.match(instagram.InstagramStoriesExtractor.pattern, sys.argv[-1])
    assert match is not None
    extractor = OfflineStoriesExtractor(match)
    extractor.fixture_kind = kind
    extractor.session = Session()
    job.DataJob(extractor, resolve=True).run()


if __name__ == "__main__":
    main()
