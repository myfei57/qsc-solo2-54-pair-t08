"""泄漏检测状态快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LeakSnapshot:
    """泄漏检测某一时刻的状态。"""

    peak_ppm: float
    threshold_ppm: float
    tripped: bool
    latch_engaged: bool
    remaining_s: float
    cycles: int

    def as_dict(self) -> dict:
        return {
            "peak_ppm": round(self.peak_ppm, 2),
            "threshold_ppm": round(self.threshold_ppm, 2),
            "tripped": self.tripped,
            "latch_engaged": self.latch_engaged,
            "remaining_s": round(self.remaining_s, 1),
            "cycles": self.cycles,
        }


@dataclass(frozen=True)
class ZoneSnapshot:
    """一个泄漏分区某一时刻的状态。"""

    name: str
    branch: str
    area: str
    recovery: str
    state: str
    cause: str
    probe_ok: bool
    ppm: float
    peak_ppm: float
    threshold_ppm: float
    tripped: bool
    latch_engaged: bool
    remaining_s: float
    cycles: int
    isolated_at: float
    incident: str
    valve_open: bool
    isolations: int

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "branch": self.branch,
            "area": self.area,
            "recovery": self.recovery,
            "state": self.state,
            "cause": self.cause,
            "probe_ok": self.probe_ok,
            "ppm": round(self.ppm, 2),
            "peak_ppm": round(self.peak_ppm, 2),
            "threshold_ppm": round(self.threshold_ppm, 2),
            "tripped": self.tripped,
            "latch_engaged": self.latch_engaged,
            "remaining_s": round(self.remaining_s, 1),
            "cycles": self.cycles,
            "isolated_at": round(self.isolated_at, 3),
            "incident": self.incident,
            "valve_open": self.valve_open,
            "isolations": self.isolations,
        }
