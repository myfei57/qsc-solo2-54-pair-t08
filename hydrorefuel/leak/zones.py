"""分物理区泄漏检测聚合。

:class:`LeakZones` 把各物理分区的 :class:`~hydrorefuel.leak.zone.LeakZone`
聚到一处，对外提供按区采样、批量采样（一轮多区采样用来区分单点还是多点
同时超标）、探头上下线、按区人工复位与整体状态汇总。

隔离与恢复的物理动作只落在超标（或探头掉线）的那一路，其他分区保持可用：
调用方在执行卸氢/增压/加注前用 :meth:`guard` 核对目标物理区是否被隔离。
多路并发超标时，每路各自记一条来源，汇总快照给出 ``tripped_zones``，
多点与单点可直接按数量区分。
"""

from __future__ import annotations

import threading

from ..errors import LeakError
from .zone import AUTO, MANUAL, LeakZone


class LeakZones:
    """按物理分区管理多路泄漏探测与隔离。"""

    def __init__(self, zones: list[LeakZone]) -> None:
        if not zones:
            raise ValueError("至少要配置一个泄漏检测分区")
        self._zones: dict[str, LeakZone] = {}
        for zone in zones:
            if zone.zone_id in self._zones:
                raise ValueError(f"泄漏分区重复: {zone.zone_id}")
            self._zones[zone.zone_id] = zone
        self._lock = threading.RLock()

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._zones)

    def zone(self, zone_id: str) -> LeakZone:
        """按标识取分区，未知分区直接报错，不允许静默落到别处。"""

        try:
            return self._zones[zone_id]
        except KeyError as error:
            raise LeakError(f"未知泄漏分区: {zone_id}", component="leak") from error

    def _sources_for(self, zone_id: str, sources) -> tuple[str, ...]:
        if sources:
            return tuple(str(item) for item in sources)
        return (zone_id,)

    def sample(
        self,
        now: float,
        zone_id: str,
        concentration_ppm: float,
        sources: tuple[str, ...] | None = None,
    ) -> dict:
        """对一个物理分区记一次采样。"""

        with self._lock:
            return self.zone(zone_id).sample(
                now, concentration_ppm, self._sources_for(zone_id, sources)
            )

    def sample_batch(self, now: float, readings: dict[str, float]) -> dict:
        """一轮内对多个分区同时采样，用来判定单点还是多点同时超标。"""

        with self._lock:
            per_zone = {}
            tripped = []
            isolated = []
            for zone_id, ppm in readings.items():
                result = self.zone(zone_id).sample(now, ppm, (zone_id,))
                per_zone[zone_id] = result
                if result["tripped"]:
                    tripped.append(zone_id)
                if result["new_isolation"]:
                    isolated.append(zone_id)
            return {
                "zones": per_zone,
                "tripped_zones": tripped,
                "new_isolated_zones": isolated,
                "simultaneous": len(tripped) > 1,
                "multi_point": len(tripped) > 1,
                "count": len(tripped),
            }

    def probe_offline(self, now: float, zone_id: str) -> dict:
        """标记某分区探头掉线：部分失败按有泄漏处理。"""

        with self._lock:
            return self.zone(zone_id).mark_offline(now)

    def probe_online(self, now: float, zone_id: str) -> dict:
        """标记某分区探头重新上线。"""

        with self._lock:
            return self.zone(zone_id).mark_online(now)

    def reset(self, now: float, zone_id: str) -> dict:
        """人工复位一个物理分区，未消干净会抛出 :class:`LeakError`。"""

        with self._lock:
            return self.zone(zone_id).manual_reset(now)

    def tick(self, now: float) -> list[str]:
        """推进所有分区的保持计时，返回本轮自动恢复的分区。"""

        with self._lock:
            return [zone_id for zone_id, zone in self._zones.items() if zone.tick(now)]

    def isolated_zones(self) -> list[str]:
        with self._lock:
            return [zone_id for zone_id, zone in self._zones.items() if zone.isolated()]

    def offline_zones(self) -> list[str]:
        with self._lock:
            return [zone_id for zone_id, zone in self._zones.items() if not zone.online()]

    def guard(self, zone_id: str) -> None:
        """操作前联锁：目标物理区被隔离（或探头掉线）即拒绝。"""

        zone = self.zone(zone_id)
        if not zone.online():
            raise LeakError(
                f"物理分区 {zone_id} 泄漏探头掉线，按有泄漏处理，本区已隔离",
                component="leak",
            )
        if zone.isolated():
            raise LeakError(
                f"物理分区 {zone_id} 泄漏隔离中，相关操作被联锁拒绝",
                component="leak",
            )

    def any_blocked(self) -> bool:
        """是否任一分区处于隔离（平台级停机判据，保守取全部分区）。"""

        return bool(self.isolated_zones())

    def total_samples(self) -> int:
        return sum(zone.samples() for zone in self._zones.values())

    def total_trips(self) -> int:
        return sum(zone.trips() for zone in self._zones.values())

    def summary(self, now: float) -> dict:
        """汇总各物理分区状态，供控制台展示与审计。"""

        with self._lock:
            zones = [zone.snapshot(now) for zone in self._zones.values()]
        tripped = [item["zone"] for item in zones if item["tripped"]]
        isolated = [item["zone"] for item in zones if item["isolated"]]
        offline = [item["zone"] for item in zones if not item["probe_online"]]
        sources: dict[str, list[str]] = {
            item["zone"]: item["sources"] for item in zones if item["sources"]
        }
        return {
            "zoning_basis": "physical_zone",
            "zones": zones,
            "zone_ids": [item["zone"] for item in zones],
            "tripped_zones": tripped,
            "isolated_zones": isolated,
            "offline_zones": offline,
            "multi_point": len(tripped) > 1,
            "simultaneous": len(tripped) > 1,
            "sources_by_zone": sources,
            "isolated_count": len(isolated),
            "blocked": bool(isolated or offline),
            "total_samples": sum(item["samples"] for item in zones),
            "total_trips": sum(item["trips"] for item in zones),
        }
