"""Laptop C telemetry receiver: two TCP listeners (A and B), validation,
duplicate/stale/gap detection, run-ID tracking and connection health.

All counters come from messages actually received. Nothing is fabricated.
"""
import asyncio
import collections
import time
from common.constants import LAPTOP_A, LAPTOP_B
from common.framing import read_frame, FramingError
from common.messages import decode_message
from common.validation import validate_telemetry_message

NEVER, CONNECTED, STALE, DISCONNECTED = "NEVER_CONNECTED", "CONNECTED", "STALE", "DISCONNECTED"


class PeerState:
    def __init__(self, name, dedupe_window):
        self.name = name
        self.connected = False
        self.ever_connected = False
        self.run_id = None
        self.last_rx = None            # time.monotonic() of last accepted message
        self.last_rx_wall = None
        self.last_seq = None
        self.accepted = 0
        self.rejected_invalid = 0
        self.rejected_duplicate = 0
        self.rejected_stale_seq = 0    # seq <= last_seq (out-of-order / replay)
        self.run_id_mismatch = 0
        self.missing_seq = 0           # gaps detected in sequence numbers
        self.connections = 0
        self.bytes_rx = 0
        self.disconnect_intervals = []  # [(disc_wall_time, reconnect_wall_time|None)]
        self._seen = collections.deque(maxlen=dedupe_window)
        self._seen_set = set()
        self.latest = {}               # latest payload per msg_type (for supervisor)

    def remember(self, mid):
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(mid)
        self._seen_set.add(mid)

    def status(self, stale_after_s, now=None):
        now = time.monotonic() if now is None else now
        if not self.ever_connected:
            return NEVER
        if not self.connected:
            return DISCONNECTED
        if self.last_rx is None or now - self.last_rx > stale_after_s:
            return STALE
        return CONNECTED


class TelemetryCollector:
    def __init__(self, cfg, run_id=None, on_message=None, event_buffer=1000):
        self.cfg = cfg
        self.run_id = run_id                 # adopted from first valid message if None
        self.on_message = on_message
        self.peers = {LAPTOP_A: PeerState(LAPTOP_A, cfg["dedupe_window"]),
                      LAPTOP_B: PeerState(LAPTOP_B, cfg["dedupe_window"])}
        self.events = collections.deque(maxlen=event_buffer)  # bounded
        self._servers = []
        self._writers = set()
        self.ports = {}

    # ---------- lifecycle ----------
    async def start(self):
        host = self.cfg["laptop_c"]["bind_host"]
        for peer, key in ((LAPTOP_A, "telemetry_port_a"), (LAPTOP_B, "telemetry_port_b")):
            srv = await asyncio.start_server(
                lambda r, w, p=peer: self._handle(p, r, w), host, self.cfg["laptop_c"][key])
            self._servers.append(srv)
            self.ports[peer] = srv.sockets[0].getsockname()[1]  # real port (supports port 0)

    async def stop(self):
        for s in self._servers:
            s.close()
        for w in list(self._writers):
            w.close()
        for s in self._servers:
            await s.wait_closed()
        self._servers.clear()

    # ---------- per-connection handler ----------
    async def _handle(self, expected, reader, writer):
        peer = self.peers[expected]
        self._writers.add(writer)
        peer.connected = peer.ever_connected = True
        peer.connections += 1
        if peer.disconnect_intervals and peer.disconnect_intervals[-1][1] is None:
            a, _ = peer.disconnect_intervals[-1]
            peer.disconnect_intervals[-1] = (a, time.time())
        self._log("CONNECT", expected, f"{expected} connected from {writer.get_extra_info('peername')}")
        maxb = self.cfg["limits"]["max_message_bytes"]
        tmo = self.cfg["limits"]["read_timeout_s"]
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(read_frame(reader, maxb), tmo)
                except asyncio.TimeoutError:
                    self._log("TIMEOUT", expected, f"no data for {tmo}s; closing")
                    break
                except FramingError as e:
                    self._log("FRAMING", expected, str(e))
                    break
                peer.bytes_rx += len(raw)
                self.process_raw(expected, raw)
        finally:
            peer.connected = False
            peer.disconnect_intervals.append((time.time(), None))
            self._writers.discard(writer)
            writer.close()
            self._log("DISCONNECT", expected, f"{expected} disconnected")

    # ---------- message processing (also directly testable) ----------
    def process_raw(self, expected, raw):
        peer = self.peers[expected]
        try:
            msg = decode_message(raw)
        except (UnicodeDecodeError, ValueError) as e:
            peer.rejected_invalid += 1
            self._log("REJECT", expected, f"undecodable message: {e}")
            return False
        return self.process_message(expected, msg)

    def process_message(self, expected, msg):
        peer = self.peers[expected]
        ok, err = validate_telemetry_message(msg, self.cfg["max_clock_skew_s"])
        if not ok:
            peer.rejected_invalid += 1
            self._log("REJECT", expected, f"invalid: {err}")
            return False
        if msg["source"] != expected:
            peer.rejected_invalid += 1
            self._log("REJECT", expected, f"source '{msg['source']}' on {expected} port")
            return False
        if msg["destination"] != "C":
            peer.rejected_invalid += 1
            self._log("REJECT", expected, "destination is not C")
            return False
        # run-ID tracking
        if self.run_id is None:
            self.run_id = msg["run_id"]
        if msg["run_id"] != self.run_id:
            peer.run_id_mismatch += 1
            self._log("REJECT", expected, f"run_id {msg['run_id']} != {self.run_id}")
            return False
        peer.run_id = msg["run_id"]
        # duplicates
        if msg["message_id"] in peer._seen_set:
            peer.rejected_duplicate += 1
            self._log("DUPLICATE", expected, f"message_id {msg['message_id'][:8]}")
            return False
        # sequence handling (stale/out-of-order vs gap)
        seq = msg.get("seq")
        if seq is not None:
            if peer.last_seq is not None:
                if seq <= peer.last_seq:
                    peer.rejected_stale_seq += 1
                    self._log("STALE", expected, f"seq {seq} <= last {peer.last_seq}")
                    return False
                if seq > peer.last_seq + 1:
                    gap = seq - peer.last_seq - 1
                    peer.missing_seq += gap
                    self._log("GAP", expected, f"{gap} message(s) missing before seq {seq}")
            peer.last_seq = seq
        peer.remember(msg["message_id"])
        peer.accepted += 1
        peer.last_rx = time.monotonic()
        peer.last_rx_wall = time.time()
        peer.latest[msg["msg_type"]] = msg["payload"]
        self.events.append({"t": peer.last_rx_wall, "kind": msg["msg_type"], "src": expected,
                            "msg_id": msg["message_id"], "seq": seq,
                            "orig_ts": msg["timestamp"], "zone": msg.get("zone_id"),
                            "panel": msg.get("panel_id"), "corr": msg.get("correlation_id")})
        if self.on_message:
            self.on_message(expected, msg)
        return True

    def _log(self, kind, src, text):
        self.events.append({"t": time.time(), "kind": kind, "src": src, "text": text})

    def health(self):
        stale = self.cfg["stale_after_s"]
        return {n: p.status(stale) for n, p in self.peers.items()}
