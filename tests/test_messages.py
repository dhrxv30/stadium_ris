import time, unittest
from common.messages import make_message, encode_message, decode_message
from common.validation import validate_message
from common.config import load_network_config, ConfigError


def good(**kw):
    return make_message("TELEMETRY_A", "A", "C", "run1", {"x": 1}, seq=0, zone_id="Z1", **kw)


class TestMessages(unittest.TestCase):
    def test_roundtrip_and_valid(self):
        m = decode_message(encode_message(good()))
        self.assertEqual(validate_message(m), (True, ""))

    def test_missing_field(self):
        m = good(); del m["run_id"]
        self.assertFalse(validate_message(m)[0])

    def test_wrong_version(self):
        m = good(); m["protocol_version"] = 99
        ok, err = validate_message(m)
        self.assertFalse(ok); self.assertIn("protocol_version", err)

    def test_bad_type_source_zone_panel(self):
        for field, val in (("msg_type", "BOGUS"), ("source", "X"), ("zone_id", "Z9"), ("panel_id", "P33")):
            m = good(); m[field] = val
            self.assertFalse(validate_message(m)[0], field)
        m = good(); m["panel_id"] = "P32"
        self.assertTrue(validate_message(m)[0])

    def test_future_timestamp_and_bool_seq(self):
        m = good(timestamp=time.time() + 10_000)
        self.assertFalse(validate_message(m)[0])
        m = good(); m["seq"] = True
        self.assertFalse(validate_message(m)[0])

    def test_non_dict(self):
        self.assertFalse(validate_message([1, 2])[0])

    def test_config_defaults_and_errors(self):
        self.assertEqual(load_network_config()["laptop_c"]["telemetry_port_a"], 9101)
        with self.assertRaises(ConfigError):
            load_network_config(None, {"laptop_c": {"telemetry_port_a": 70000}})
        with self.assertRaises(ConfigError):
            load_network_config("does_not_exist.yaml")
        self.assertEqual(load_network_config("config/network.yaml")["protocol_version"], 1)


if __name__ == "__main__":
    unittest.main()
