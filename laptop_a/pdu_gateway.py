"""
pdu_gateway.py — Per-Zone Bounded PDU Queues with Priority Scheduling

Manages uplink and downlink PDU queues for all 5 stadium zones.
Implements bounded buffers, configurable drop policies, priority-aware
enqueue, and strict accounting so that:

    Generated == Delivered + Dropped + Pending  (always)

Drop policies:
  • TAIL_DROP    — newest packet is dropped when queue is full.
  • HEAD_DROP   — oldest packet is dropped when queue is full.
  • PRIORITY_DROP — lowest-priority packet in the queue is evicted.

Scheduling:
  • ROUND_ROBIN  — equal turns across zones.
  • WEIGHTED     — proportional to configurable zone weights.
  • PRIORITY     — dequeue highest-priority PDU first.

Integration points:
  → TrafficGenerator pushes UL PDUs here.
  → main.py dequeues UL PDUs and sends to Laptop B (data-plane TCP).
  → Laptop B sends DL PDUs that main.py enqueues here.
  → ReceiverModel evaluates DL PDUs dequeued for delivery.
  → MetricsRegistry tracks all queue events.
  → Laptop B scheduling updates adjust zone weights dynamically.
"""

import time
from enum import Enum
from collections import deque, defaultdict
from typing import Dict, List, Optional, Tuple

try:
    from metrics import MetricsRegistry
except ImportError:
    from laptop_a.metrics import MetricsRegistry


# ---------------------------------------------------------------------------
#  Configuration enums
# ---------------------------------------------------------------------------

class DropPolicy(str, Enum):
    TAIL_DROP     = "tail_drop"
    HEAD_DROP     = "head_drop"
    PRIORITY_DROP = "priority_drop"


class SchedulePolicy(str, Enum):
    ROUND_ROBIN = "round_robin"
    WEIGHTED    = "weighted"
    PRIORITY    = "priority"


# ---------------------------------------------------------------------------
#  Single Bounded Queue
# ---------------------------------------------------------------------------

class BoundedPDUQueue:
    """
    A bounded FIFO queue with a configurable drop policy.

    Maintains internal counters for enqueued, dequeued, and dropped PDUs.
    """

    def __init__(self, max_size: int = 10_000, drop_policy: DropPolicy = DropPolicy.TAIL_DROP):
        self.max_size = max_size
        self.drop_policy = drop_policy
        self._queue: deque = deque()

        # Accounting
        self.enqueued: int = 0
        self.dequeued: int = 0
        self.dropped: int = 0

    @property
    def depth(self) -> int:
        return len(self._queue)

    @property
    def is_full(self) -> bool:
        return len(self._queue) >= self.max_size

    def enqueue(self, pdu: dict) -> Tuple[bool, Optional[dict]]:
        """
        Enqueue a PDU.

        Returns:
            (accepted, dropped_pdu)
            - accepted=True:  pdu was added; dropped_pdu is None.
            - accepted=False: pdu (or a victim) was dropped; dropped_pdu is the one discarded.
        """
        if not self.is_full:
            self._queue.append(pdu)
            if not pdu.get("status") or pdu.get("status") == "generated":
                pdu["status"] = "queued"
            self.enqueued += 1
            return True, None

        # Queue full — apply drop policy
        if self.drop_policy == DropPolicy.TAIL_DROP:
            # Drop the incoming PDU
            pdu["status"] = "dropped"
            self.dropped += 1
            return False, pdu

        elif self.drop_policy == DropPolicy.HEAD_DROP:
            # Drop the oldest PDU, accept the new one
            victim = self._queue.popleft()
            victim["status"] = "dropped"
            self._queue.append(pdu)
            if not pdu.get("status") or pdu.get("status") == "generated":
                pdu["status"] = "queued"
            self.enqueued += 1
            self.dropped += 1
            return True, victim

        elif self.drop_policy == DropPolicy.PRIORITY_DROP:
            # Find the lowest-priority PDU in the queue (highest priority number)
            worst_idx = 0
            worst_prio = self._queue[0].get("priority", 2)
            for i, p in enumerate(self._queue):
                if p.get("priority", 2) > worst_prio:
                    worst_prio = p.get("priority", 2)
                    worst_idx = i

            incoming_prio = pdu.get("priority", 2)
            if incoming_prio < worst_prio:
                # Incoming has higher priority — evict the worst
                victim = self._queue[worst_idx]
                del self._queue[worst_idx]
                victim["status"] = "dropped"
                self._queue.append(pdu)
                if not pdu.get("status") or pdu.get("status") == "generated":
                    pdu["status"] = "queued"
                self.enqueued += 1
                self.dropped += 1
                return True, victim
            else:
                # Incoming has equal or lower priority — drop it
                pdu["status"] = "dropped"
                self.dropped += 1
                return False, pdu

        # Fallback
        pdu["status"] = "dropped"
        self.dropped += 1
        return False, pdu

    def dequeue(self, count: int = 1) -> List[dict]:
        """Dequeue up to `count` PDUs (FIFO order)."""
        result = []
        for _ in range(min(count, len(self._queue))):
            pdu = self._queue.popleft()
            pdu["status"] = "dequeued"
            self.dequeued += 1
            result.append(pdu)
        return result

    def dequeue_by_priority(self, count: int = 1) -> List[dict]:
        """Dequeue up to `count` PDUs, highest priority (lowest number) first."""
        if not self._queue:
            return []

        indexed = sorted(enumerate(self._queue), key=lambda x: x[1].get("priority", 2))
        to_remove = []
        result = []
        for idx, pdu in indexed[:count]:
            pdu["status"] = "dequeued"
            self.dequeued += 1
            result.append(pdu)
            to_remove.append(idx)

        # Remove in reverse order to preserve indices
        for idx in sorted(to_remove, reverse=True):
            del self._queue[idx]

        return result

    def peek(self, count: int = 1) -> List[dict]:
        """Peek at up to `count` PDUs without removing them."""
        return list(self._queue)[:count]

    def clear(self) -> int:
        """Drop all PDUs currently in the queue.  Returns count dropped."""
        n = len(self._queue)
        for pdu in self._queue:
            pdu["status"] = "dropped"
        self._queue.clear()
        self.dropped += n
        return n

    def stats(self) -> dict:
        return {
            "depth": self.depth,
            "max_size": self.max_size,
            "enqueued": self.enqueued,
            "dequeued": self.dequeued,
            "dropped": self.dropped,
            "utilisation": round(self.depth / self.max_size, 3) if self.max_size else 0.0,
        }


# ---------------------------------------------------------------------------
#  PDU Gateway — manages per-zone UL + DL queues
# ---------------------------------------------------------------------------

class PDUGateway:
    """
    Central PDU queueing and scheduling gateway for all 5 zones.

    Provides separate uplink and downlink queues per zone, and exposes
    scheduling methods for batched dequeue operations.

    Args:
        num_zones:       Number of zones (default 5).
        ul_queue_size:   Maximum UL queue depth per zone.
        dl_queue_size:   Maximum DL queue depth per zone.
        drop_policy:     Default drop policy for all queues.
        schedule_policy: Default scheduling policy for dequeue operations.
        metrics:         Optional MetricsRegistry for counter integration.
        zone_weights:    Optional per-zone scheduling weights (for WEIGHTED policy).
    """

    def __init__(
        self,
        num_zones: int = 5,
        ul_queue_size: int = 10_000,
        dl_queue_size: int = 10_000,
        drop_policy: DropPolicy = DropPolicy.TAIL_DROP,
        schedule_policy: SchedulePolicy = SchedulePolicy.ROUND_ROBIN,
        metrics: Optional[MetricsRegistry] = None,
        zone_weights: Optional[Dict[int, float]] = None,
    ):
        self.num_zones = num_zones
        self.schedule_policy = schedule_policy
        self.metrics = metrics

        # Per-zone queues
        self.ul_queues: Dict[int, BoundedPDUQueue] = {
            z: BoundedPDUQueue(max_size=ul_queue_size, drop_policy=drop_policy)
            for z in range(num_zones)
        }
        self.dl_queues: Dict[int, BoundedPDUQueue] = {
            z: BoundedPDUQueue(max_size=dl_queue_size, drop_policy=drop_policy)
            for z in range(num_zones)
        }

        # Scheduling weights (normalised)
        if zone_weights:
            self.zone_weights = self._normalise(zone_weights)
        else:
            self.zone_weights = {z: 1.0 / num_zones for z in range(num_zones)}

        # Round-robin state
        self._rr_index: int = 0

        # Retransmission buffer (PDU id → retry count)
        self._retransmit_counts: Dict[int, int] = {}
        self.max_retransmissions: int = 3

    @staticmethod
    def _normalise(weights: Dict[int, float]) -> Dict[int, float]:
        total = sum(weights.values())
        if total <= 0:
            n = len(weights)
            return {z: 1.0 / n for z in weights}
        return {z: w / total for z, w in weights.items()}

    # ----- enqueue -----

    def enqueue_uplink(self, pdu: dict) -> bool:
        """Enqueue an uplink PDU into the appropriate zone queue."""
        zone = pdu.get("src_zone", 0)
        accepted, dropped = self.ul_queues[zone].enqueue(pdu)

        if self.metrics:
            if accepted:
                self.metrics.increment("pdu_queued")
                self.metrics.zone_increment(zone, "pdu_queued")
            if dropped:
                self.metrics.record_drop(f"ul_queue_full_z{zone}")
                self.metrics.zone_increment(
                    dropped.get("src_zone", zone), "pdu_dropped"
                )

        return accepted

    def enqueue_downlink(self, pdu: dict) -> bool:
        """Enqueue a downlink PDU for delivery to the intended receiver."""
        zone = pdu.get("dst_zone", 0)
        accepted, dropped = self.dl_queues[zone].enqueue(pdu)

        if self.metrics:
            if accepted:
                self.metrics.increment("pdu_dl_queued")
                self.metrics.zone_increment(zone, "pdu_dl_queued")
            if dropped:
                self.metrics.record_drop(f"dl_queue_full_z{zone}")
                self.metrics.zone_increment(
                    dropped.get("dst_zone", zone), "pdu_dropped"
                )

        return accepted

    # ----- dequeue -----

    def dequeue_uplink_batch(self, total_count: int = 500) -> List[dict]:
        """Dequeue UL PDUs from all zones using the configured schedule policy."""
        return self._scheduled_dequeue(self.ul_queues, total_count)

    def dequeue_downlink_batch(self, total_count: int = 500) -> List[dict]:
        """Dequeue DL PDUs from all zones for delivery evaluation."""
        pdus = self._scheduled_dequeue(self.dl_queues, total_count)
        if self.metrics:
            self.metrics.increment("pdu_dequeued", len(pdus))
        return pdus

    def dequeue_uplink_zone(self, zone_id: int, count: int = 100) -> List[dict]:
        """Dequeue from a specific zone's UL queue."""
        return self.ul_queues[zone_id].dequeue(count)

    def dequeue_downlink_zone(self, zone_id: int, count: int = 100) -> List[dict]:
        """Dequeue from a specific zone's DL queue."""
        pdus = self.dl_queues[zone_id].dequeue(count)
        if self.metrics:
            self.metrics.increment("pdu_dequeued", len(pdus))
        return pdus

    def _scheduled_dequeue(
        self, queues: Dict[int, BoundedPDUQueue], total_count: int
    ) -> List[dict]:
        """Dequeue across zones using the active scheduling policy."""
        result: List[dict] = []

        if self.schedule_policy == SchedulePolicy.ROUND_ROBIN:
            per_zone = max(1, total_count // self.num_zones)
            for _ in range(self.num_zones):
                z = self._rr_index % self.num_zones
                self._rr_index += 1
                result.extend(queues[z].dequeue(per_zone))

        elif self.schedule_policy == SchedulePolicy.WEIGHTED:
            for z in range(self.num_zones):
                n = max(1, int(total_count * self.zone_weights.get(z, 0.2)))
                result.extend(queues[z].dequeue(n))

        elif self.schedule_policy == SchedulePolicy.PRIORITY:
            for z in range(self.num_zones):
                per_zone = max(1, total_count // self.num_zones)
                result.extend(queues[z].dequeue_by_priority(per_zone))

        return result[:total_count]

    # ----- scheduling configuration (hot-swappable by Laptop B) -----

    def set_schedule_policy(self, policy: SchedulePolicy) -> None:
        self.schedule_policy = policy

    def set_zone_weights(self, weights: Dict[int, float]) -> None:
        """Update zone scheduling weights (e.g., from Laptop B congestion decisions)."""
        self.zone_weights = self._normalise(weights)

    def set_queue_sizes(self, ul_size: int, dl_size: int) -> None:
        """Resize all queues (new size applies to future enqueues)."""
        for q in self.ul_queues.values():
            q.max_size = ul_size
        for q in self.dl_queues.values():
            q.max_size = dl_size

    # ----- retransmission support -----

    def retransmit_pdu(self, pdu: dict) -> bool:
        """Public wrapper used by the Laptop A runtime for failed DL retransmission attempts."""
        return self.enqueue_retransmission(pdu)

    def enqueue_retransmission(self, pdu: dict) -> bool:
        """Re-enqueue a failed DL PDU for retransmission, if under the retry limit."""
        pdu_id = pdu.get("pdu_id", -1)
        count = self._retransmit_counts.get(pdu_id, 0)

        if count >= self.max_retransmissions:
            pdu["status"] = "dropped"
            if self.metrics:
                self.metrics.record_drop("max_retransmissions")
                self.metrics.zone_increment(pdu.get("dst_zone", 0), "pdu_dropped")
            return False

        self._retransmit_counts[pdu_id] = count + 1
        pdu["status"] = "retransmit_queued"
        pdu["retransmit_count"] = count + 1
        zone = pdu.get("dst_zone", 0)
        accepted, dropped = self.dl_queues[zone].enqueue(pdu)

        if self.metrics:
            if accepted:
                self.metrics.increment("pdu_retransmitted")
                self.metrics.zone_increment(zone, "pdu_retransmitted")
            elif dropped:
                self.metrics.record_drop(f"retx_queue_full_z{zone}")
                self.metrics.zone_increment(zone, "pdu_dropped")

        return accepted

    # ----- queue introspection -----

    def get_ul_depth(self, zone_id: int) -> int:
        return self.ul_queues[zone_id].depth

    def get_dl_depth(self, zone_id: int) -> int:
        return self.dl_queues[zone_id].depth

    def get_total_pending(self) -> int:
        return sum(q.depth for q in self.ul_queues.values()) + \
               sum(q.depth for q in self.dl_queues.values())

    def get_queue_stats(self) -> dict:
        """Full queue statistics for telemetry."""
        return {
            "uplink": {f"Z{z+1}": self.ul_queues[z].stats() for z in range(self.num_zones)},
            "downlink": {f"Z{z+1}": self.dl_queues[z].stats() for z in range(self.num_zones)},
            "total_pending": self.get_total_pending(),
            "schedule_policy": self.schedule_policy.value,
            "zone_weights": {f"Z{z+1}": round(w, 3) for z, w in self.zone_weights.items()},
            "retransmissions_tracked": len(self._retransmit_counts),
        }

    # ----- update metrics queue-depth snapshot -----

    def update_metrics_queue_depths(self) -> None:
        """Push current queue depths into the MetricsRegistry."""
        if not self.metrics:
            return
        for z in range(self.num_zones):
            self.metrics.set_queue_depth(f"ul_z{z}", self.ul_queues[z].depth)
            self.metrics.set_queue_depth(f"dl_z{z}", self.dl_queues[z].depth)
