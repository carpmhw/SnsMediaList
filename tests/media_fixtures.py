"""僅含自行產生的可解碼媒體測試資料，不讀取平台內容。"""

import struct
import zlib


def original_png() -> bytes:
    """建立真正可解碼的 1600×1000 合成原圖。"""

    def chunk(kind: bytes, body: bytes) -> bytes:
        """以標準長度與 CRC 包裝 PNG 區塊。"""
        return (
            struct.pack("!I", len(body)) + kind + body + struct.pack("!I", zlib.crc32(kind + body))
        )

    pixels = (b"\x00" + b"\x40\x80\x60" * 1600) * 1000
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", 1600, 1000, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )
