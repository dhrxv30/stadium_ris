import unittest, struct
from common.framing import FrameDecoder, encode_frame, FramingError


class TestFraming(unittest.TestCase):
    def test_roundtrip(self):
        d = FrameDecoder()
        self.assertEqual(d.feed(encode_frame(b"hello")), [b"hello"])

    def test_fragmented_byte_by_byte(self):
        d, out = FrameDecoder(), []
        for b in encode_frame(b'{"a":1}'):
            out += d.feed(bytes([b]))
        self.assertEqual(out, [b'{"a":1}'])
        self.assertEqual(d.pending_bytes, 0)

    def test_combined_frames(self):
        d = FrameDecoder()
        data = encode_frame(b"one") + encode_frame(b"two") + encode_frame(b"three")
        self.assertEqual(d.feed(data), [b"one", b"two", b"three"])

    def test_combined_plus_partial(self):
        d = FrameDecoder()
        data = encode_frame(b"one") + encode_frame(b"two")
        self.assertEqual(d.feed(data[:-2]), [b"one"])
        self.assertEqual(d.feed(data[-2:]), [b"two"])

    def test_oversize_rejected(self):
        d = FrameDecoder(max_bytes=10)
        with self.assertRaises(FramingError):
            d.feed(struct.pack(">I", 11) + b"x" * 11)
        with self.assertRaises(FramingError):
            encode_frame(b"x" * 11, max_bytes=10)

    def test_empty_payload(self):
        self.assertEqual(FrameDecoder().feed(encode_frame(b"")), [b""])


if __name__ == "__main__":
    unittest.main()
