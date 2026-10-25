import pytest

from common.framing import FrameDecoder, FramingError, encode_frame


def test_roundtrip_single():
    assert FrameDecoder().feed(encode_frame(b"hello")) == [b"hello"]


def test_combined_frames_in_one_chunk():
    data = encode_frame(b"a") + encode_frame(b"bb") + encode_frame(b"ccc")
    assert FrameDecoder().feed(data) == [b"a", b"bb", b"ccc"]


def test_fragmented_byte_by_byte():
    data = encode_frame(b"fragmented-payload") + encode_frame(b"second")
    dec, out = FrameDecoder(), []
    for i in range(len(data)):
        out += dec.feed(data[i:i + 1])
    assert out == [b"fragmented-payload", b"second"]
    assert dec.buffered == 0


def test_oversize_rejected_on_encode_and_decode():
    with pytest.raises(FramingError):
        encode_frame(b"x" * 11, max_bytes=10)
    big = encode_frame(b"x" * 11, max_bytes=100)
    with pytest.raises(FramingError):
        FrameDecoder(max_bytes=10).feed(big)


def test_partial_frame_waits():
    dec = FrameDecoder()
    assert dec.feed(encode_frame(b"abcdef")[:5]) == []
    assert dec.buffered == 5
