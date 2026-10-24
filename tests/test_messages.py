import copy
import json

import pytest

from common.constants import MSG_FEEDBACK_REPORT, MSG_HEARTBEAT
from common.messages import MessageError, decode_message, encode_message, make_message
from common.validation import ValidationError, validate_message

PAYLOAD = {"seq": 1, "window_start": 1.0, "window_end": 2.0, "queue_occupancy": 10,
           "queue_capacity": 100, "generated": 50, "delivered": 40, "dropped": 5}


def good():
    return make_message(MSG_FEEDBACK_REPORT, "A", "B", "run1", copy.deepcopy(PAYLOAD), zone_id="Z1")


def test_encode_decode_roundtrip():
    msg = good()
    raw = encode_message(msg)[4:]            # strip frame header
    assert decode_message(raw) == msg


def test_valid_feedback_and_heartbeat_pass():
    validate_message(good())
    validate_message(make_message(MSG_HEARTBEAT, "A", "B", "run1", {"seq": 3}))


def test_optional_measurements_may_be_null():
    m = good()
    m["payload"].update(mean_sinr_db=None, per=None)
    validate_message(m)


@pytest.mark.parametrize("mutate", [
    lambda m: m.pop("msg_id"),
    lambda m: m.update(version=2),
    lambda m: m.update(src="X"),
    lambda m: m.update(zone_id="Z9"),
    lambda m: m.pop("zone_id"),
    lambda m: m.update(type="NOPE"),
    lambda m: m["payload"].pop("delivered"),
    lambda m: m["payload"].update(seq="1"),
    lambda m: m["payload"].update(seq=True),
    lambda m: m["payload"].update(per=1.5),
    lambda m: m["payload"].update(queue_occupancy=101),
    lambda m: m["payload"].update(window_end=0.5),
    lambda m: m.update(panel_id="P33"),
])
def test_invalid_messages_rejected(mutate):
    m = good()
    mutate(m)
    with pytest.raises(ValidationError):
        validate_message(m)


def test_decode_rejects_garbage():
    for raw in (b"{not json", b"\xff\xfe", b"[1,2,3]"):
        with pytest.raises(MessageError):
            decode_message(raw)


def test_nan_not_serialisable():
    m = good()
    m["payload"]["per"] = float("nan")
    with pytest.raises(ValueError):
        encode_message(m)
    assert json.loads(json.dumps({"a": 1}))
