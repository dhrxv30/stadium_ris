import struct

import pytest

from common.framing import FrameDecoder, FramingError, encode_frame


def test_roundtrip_single():
    assert FrameDecoder().feed(encode_frame(b"hello")) == [b"hello"]


def test_combined_frames_in_one_chunk():
    data = encode_frame(b"a") + encode_frame(b"bb") + encode_frame(b"ccc")
    assert FrameDecoder().feed(data) == [b"a", b"bb", b"ccc"]


def test_fragmented_byte_by_byte():
    data = encode_frame(b"fragmented-payload") + encode_frame(b"second")
    decoder, output = FrameDecoder(), []
    for byte in data:
        output.extend(decoder.feed(bytes((byte,))))
    assert output == [b"fragmented-payload", b"second"]
    assert decoder.buffered == decoder.pending_bytes == 0


def test_oversize_rejected_on_encode_and_decode():
    with pytest.raises(FramingError):
        encode_frame(b"x" * 11, max_bytes=10)
    with pytest.raises(FramingError):
        FrameDecoder(max_bytes=10).feed(struct.pack(">I", 11) + b"x" * 11)


def test_partial_frame_waits():
    decoder = FrameDecoder()
    assert decoder.feed(encode_frame(b"abcdef")[:5]) == []
    assert decoder.buffered == 5
