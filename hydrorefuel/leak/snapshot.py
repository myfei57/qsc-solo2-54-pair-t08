"""泄漏分区状态快照。

快照是只读的传输结构：每个物理分区一份 :class:`LeakZoneSnapshot`，汇总侧
:class:`LeakZonesSnapshot` 额外带上分区口径（定死为物理分区）与多路并发
归因字段，控制台展示与审计落盘共用同一份口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LeakZoneSnapshot:
    """单个物理分区某一时刻的泄漏隔离状态。"""

    zone: str
    label: str
    recovery: str
    probe_online: bool
    ppm: float
    peak_ppm: float
    threshold_ppm: float
    samples: int
    tripped: bool
    isolated: bool
    leaking: bool
    hold_remaining_s: float
    hold_elapsed: bool
    sources: tuple[str, ...] = ()
    trip_reason: str = ""
    trips: int = 0
    manual_resets: int = 0
    auto_recoveries: int = 0
    ready: bool = True

    def as_dict(self) -> dict:
        return {
            "zone": self.zone,
            "label": self.label,
            "recovery": self.recovery,
            "probe_online": self.probe_online,
            "ppm": round(self.ppm, 2),
            "peak_ppm": round(self.peak_ppm, 2),
            "threshold_ppm": round(self.threshold_ppm, 2),
            "samples": self.samples,
            "tripped": self.tripped,
            "isolated": self.isolated,
            "leaking": self.leaking,
            "hold_remaining_s": round(self.hold_remaining_s, 1),
            "hold_elapsed": self.hold_elapsed,
            "sources": list(self.sources),
            "trip_reason": self.trip_reason,
            "trips": self.trips,
            "manual_resets": self.manual_resets,
            "auto_recoveries": self.auto_recoveries,
            "ready": self.ready,
        }


@dataclass(frozen=True)
class LeakZonesSnapshot:
    """全平台泄漏分区的汇总快照。"""

    zoning_basis: str
    zones: tuple[LeakZoneSnapshot, ...] = field(default_factory=tuple)
    tripped_zones: tuple[str, ...] = ()
    isolated_zones: tuple[str, ...] = ()
    offline_zones: tuple[str, ...] = ()
    multi_point: bool = False

    def as_dict(self) -> dict:
        zones = [zone.as_dict() for zone in self.zones]
        return {
            "zoning_basis": self.zoning_basis,
            "zones": zones,
            "zone_ids": [zone.zone for zone in zones],
            "tripped_zones": list(self.tripped_zones),
            "isolated_zones": list(self.isolated_zones),
            "offline_zones": list(self.offline_zones),
            "multi_point": self.multi_point,
            "simultaneous": self.multi_point,
            "isolated_count": len(self.isolated_zones),
            "blocked": bool(self.isolated_zones or self.offline_zones),
            "total_samples": sum(zone.samples for zone in self.zones),
            "total_trips": sum(zone.trips for zone in self.zones),
        }
