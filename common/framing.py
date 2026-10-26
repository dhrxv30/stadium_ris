"""Length-prefixed framing for TCP byte streams."""

import asyncio
import struct

from common.constants import DEFAULT_MAX_MESSAGE_BYTES, LENGTH_PREFIX_BYTES

_HEADER = struct.Struct(">I")


class FramingError(ValueError):
    """Raised when a frame is malformed or exceeds the configured limit."""


def encode_frame(payload: bytes, max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> bytes:
    if len(payload) > max_bytes:
        raise FramingError(f"payload {len(payload)} B exceeds max {max_bytes} B")
    return _HEADER.pack(len(payload)) + payload


class FrameDecoder:
    """Incrementally decode complete frames from arbitrary TCP chunks."""

    def __init__(self, max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES):
        self.max_bytes = max_bytes
        self._buf = bytearray()

    def feed(self, data: bytes) -> list:
        self._buf.extend(data)
        frames = []
        while len(self._buf) >= LENGTH_PREFIX_BYTES:
            (length,) = _HEADER.unpack_from(self._buf, 0)
            if length > self.max_bytes:
                raise FramingError(
                    f"frame length {length} exceeds max {self.max_bytes}"
                )
            frame_end = LENGTH_PREFIX_BYTES + length
            if len(self._buf) < frame_end:
                break
            frames.append(bytes(self._buf[LENGTH_PREFIX_BYTES:frame_end]))
            del self._buf[:frame_end]
        return frames

    @property
    def buffered(self) -> int:
        return len(self._buf)

    @property
    def pending_bytes(self) -> int:
        return len(self._buf)


async def read_frame(
    reader: asyncio.StreamReader,
    max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> bytes:
    """Read exactly one framed payload from an asyncio stream."""
    try:
        header = await reader.readexactly(LENGTH_PREFIX_BYTES)
        (length,) = _HEADER.unpack(header)
        if length > max_bytes:
            raise FramingError(
                f"declared length {length} exceeds max {max_bytes}"
            )
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise FramingError("connection closed mid-frame") from exc
