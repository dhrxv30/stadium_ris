"""Length-prefixed framing for TCP (a byte stream with no message boundaries).

Frame = 4-byte big-endian payload length + payload bytes (UTF-8 JSON).
"""
import asyncio
import struct
from .constants import LENGTH_PREFIX_BYTES, MAX_MESSAGE_BYTES_DEFAULT


class FramingError(Exception):
    """Raised for oversize or truncated frames."""


def encode_frame(payload: bytes, max_bytes: int = MAX_MESSAGE_BYTES_DEFAULT) -> bytes:
    if len(payload) > max_bytes:
        raise FramingError(f"payload {len(payload)} B exceeds max {max_bytes} B")
    return struct.pack(">I", len(payload)) + payload


class FrameDecoder:
    """Incremental decoder: feed arbitrary byte chunks, get complete payloads.

    Handles fragmented frames and several frames combined in one chunk.
    """

    def __init__(self, max_bytes: int = MAX_MESSAGE_BYTES_DEFAULT):
        self.max_bytes = max_bytes
        self._buf = bytearray()

    def feed(self, data: bytes) -> list:
        self._buf.extend(data)
        out = []
        while True:
            if len(self._buf) < LENGTH_PREFIX_BYTES:
                break
            (n,) = struct.unpack(">I", self._buf[:LENGTH_PREFIX_BYTES])
            if n > self.max_bytes:
                self._buf.clear()
                raise FramingError(f"declared length {n} exceeds max {self.max_bytes}")
            if len(self._buf) < LENGTH_PREFIX_BYTES + n:
                break
            out.append(bytes(self._buf[LENGTH_PREFIX_BYTES:LENGTH_PREFIX_BYTES + n]))
            del self._buf[:LENGTH_PREFIX_BYTES + n]
        return out

    @property
    def pending_bytes(self) -> int:
        return len(self._buf)


async def read_frame(reader: asyncio.StreamReader,
                     max_bytes: int = MAX_MESSAGE_BYTES_DEFAULT) -> bytes:
    """Read exactly one frame from an asyncio stream."""
    try:
        header = await reader.readexactly(LENGTH_PREFIX_BYTES)
        (n,) = struct.unpack(">I", header)
        if n > max_bytes:
            raise FramingError(f"declared length {n} exceeds max {max_bytes}")
        return await reader.readexactly(n)
    except asyncio.IncompleteReadError as e:
        raise FramingError("connection closed mid-frame") from e
