"""Length-prefixed framing for TCP (a byte stream with no message boundaries).

Frame = 4-byte big-endian unsigned length N, followed by N payload bytes.
"""
import struct

from common.constants import DEFAULT_MAX_MESSAGE_BYTES

_HDR = struct.Struct(">I")


class FramingError(ValueError):
    """Raised when a frame header is invalid (stream cannot be resynchronised)."""


def encode_frame(payload: bytes, max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> bytes:
    if len(payload) > max_bytes:
        raise FramingError(f"payload {len(payload)} B exceeds max {max_bytes} B")
    return _HDR.pack(len(payload)) + payload


class FrameDecoder:
    """Incremental decoder: feed() arbitrary TCP chunks, get back complete frames."""

    def __init__(self, max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES):
        self.max_bytes = max_bytes
        self._buf = bytearray()

    def feed(self, data: bytes) -> list:
        self._buf.extend(data)
        frames = []
        while len(self._buf) >= _HDR.size:
            (n,) = _HDR.unpack_from(self._buf, 0)
            if n > self.max_bytes:
                raise FramingError(f"frame length {n} exceeds max {self.max_bytes}")
            if len(self._buf) < _HDR.size + n:
                break
            frames.append(bytes(self._buf[_HDR.size:_HDR.size + n]))
            del self._buf[:_HDR.size + n]
        return frames

    @property
    def buffered(self) -> int:
        return len(self._buf)
