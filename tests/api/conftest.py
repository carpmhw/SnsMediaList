"""API 測試對獨立 application logger 的觀測介面。"""

import logging
from collections.abc import Iterator

import pytest


@pytest.fixture
def caplog(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """直接捕捉 application logger，不依賴已停用的 root propagation。"""
    logger = logging.getLogger("sns_media_list")
    propagate = logger.propagate
    logger.propagate = False
    logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)
        logger.propagate = propagate
