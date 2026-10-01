"""泄漏分区控制：采样判定、隔离阀动作与审计一致。

:class:`LeakZoneController` 站在平台与分区状态机之间，保证三件事口径一致：

1. **隔离动作**：分区一旦确认超标或探头掉线（部分失败按有泄漏处理），立即
   关本区隔离阀并落告警审计；只关这一路，其他分区继续可用。
2. **恢复动作**：自动恢复区在保持期走完且泄漏消干净后由控制周期自动开阀；
   人工复位区只能经显式复位开阀，且未消干净一律拒绝。自动与人工恢复都各
   记一条审计。
3. **审计来源**：所有采样、隔离、上下线、恢复事件走同一条事件总线，动作
   与审计不会出现"阀动了没记录"或"记录了阀没动"的分叉。
"""

from __future__ import annotations

from ..clock import Clock
from ..config import LeakLimits
from ..eventbus import Event, EventBus
from .valve import LeakIsolationValve
from .zone import AUTO, LeakZone
from .zones import LeakZones

# 单探头旧接口与平台级联锁默认落在槽车卸氢物理区。
DEFAULT_LEAK_ZONE = "unload"


class LeakZoneController:
    """把分区状态机、隔离阀与事件审计绑成一套一致动作。"""

    def __init__(self, clock: Clock, bus: EventBus, limits: LeakLimits) -> None:
        self._clock = clock
        self._bus = bus
        self._limits = limits
        zones = [
            LeakZone(
                zone.zone_id,
                zone.label,
                limits.threshold_ppm,
                limits.hold_s,
                AUTO if zone.auto_reset else "manual",
            )
            for zone in limits.zones
        ]
        self._zones = LeakZones(zones)
        self._valves: dict[str, LeakIsolationValve] = {
            zone.zone_id: LeakIsolationValve(zone.zone_id, zone.label) for zone in limits.zones
        }

    @property
    def zones(self) -> LeakZones:
        return self._zones

    def ids(self) -> tuple[str, ...]:
        return self._zones.ids

    def _publish(self, action: str, *, severity: str, zone: str, **payload: object) -> None:
        self._bus.publish(
            Event.of("leak", "leak", action, severity=severity, zone=zone, **payload)
        )

    def _close_valve(self, zone_id: str, reason: str, sources: list[str]) -> None:
        self._valves[zone_id].close()
        zone = self._zones.zone(zone_id)
        self._publish(
            "zone_isolated",
            severity="alarm",
            zone=zone_id,
            reason=reason,
            sources=sources,
            peak_ppm=round(zone.peak_ppm(), 2),
            threshold_ppm=self._limits.threshold_ppm,
        )

    def _open_valve(self, zone_id: str, action: str) -> None:
        self._valves[zone_id].reopen()
        self._publish(
            action,
            severity="notice",
            zone=zone_id,
            manual_resets=self._zones.zone(zone_id).resets(),
            auto_recoveries=self._zones.zone(zone_id).auto_recoveries(),
        )

    def _settle_recovery(self, zone_id: str, result: dict) -> None:
        if result.get("new_isolation"):
            reason = str(result.get("reason", "leak"))
            sources = list(result.get("sources", ())) or [zone_id]
            self._close_valve(zone_id, reason, sources)
        if result.get("auto_recovered"):
            self._open_valve(zone_id, "zone_auto_recovered")

    def sample_zone(self, zone_id: str, concentration_ppm: float, sources: tuple[str, ...] | None = None) -> dict:
        """对一个物理分区采样，按结果驱动隔离阀并落审计。"""

        result = self._zones.sample(self._clock.now(), zone_id, concentration_ppm, sources)
        result["reason"] = "leak"
        result["sources"] = list(sources) if sources else [zone_id]
        self._publish(
            "sample",
            severity="alarm" if result["tripped"] else "notice",
            zone=zone_id,
            ppm=result["ppm"],
            threshold_ppm=result["threshold_ppm"],
            probe_online=True,
        )
        self._settle_recovery(zone_id, result)
        return result

    def sample_batch(self, readings: dict[str, float]) -> dict:
        """一轮多区同时采样，区分单点与多点同时超标，逐区落实隔离。"""

        now = self._clock.now()
        report = self._zones.sample_batch(now, readings)
        for zone_id, result in report["zones"].items():
            self._publish(
                "sample",
                severity="alarm" if result["tripped"] else "notice",
                zone=zone_id,
                ppm=result["ppm"],
                threshold_ppm=result["threshold_ppm"],
                probe_online=True,
                simultaneous=report["simultaneous"],
            )
            if result["new_isolation"]:
                self._close_valve(zone_id, "leak", [zone_id])
        if report["tripped_zones"]:
            self._publish(
                "leak_sources",
                severity="alarm",
                zone="",
                tripped_zones=report["tripped_zones"],
                multi_point=report["multi_point"],
                count=report["count"],
            )
        return report

    def probe_offline(self, zone_id: str) -> dict:
        """探头掉线：部分失败按有泄漏处理，立即隔离本区。"""

        result = self._zones.probe_offline(self._clock.now(), zone_id)
        self._publish(
            "probe_offline",
            severity="alarm",
            zone=zone_id,
            isolated=result["isolated"],
            message=f"分区 {zone_id} 泄漏探头掉线，按有泄漏处理",
        )
        if result["new_isolation"]:
            self._close_valve(zone_id, "probe_offline", ["probe_offline"])
        return result

    def probe_online(self, zone_id: str) -> dict:
        """探头重新上线；若恢复条件满足（仅自动恢复区）则自动开阀。"""

        result = self._zones.probe_online(self._clock.now(), zone_id)
        self._publish("probe_online", severity="notice", zone=zone_id, isolated=result["isolated"])
        if result.get("auto_recovered"):
            self._open_valve(zone_id, "zone_auto_recovered")
        return result

    def reset_zone(self, zone_id: str) -> dict:
        """人工复位一个物理分区，未消干净时抛出 :class:`LeakError`。"""

        result = self._zones.reset(self._clock.now(), zone_id)
        if result.get("reset"):
            self._open_valve(zone_id, "zone_manual_reset")
        return result

    def tick(self) -> list[str]:
        """控制周期调用：保持计时到期后自动恢复满足条件的分区。"""

        recovered = self._zones.tick(self._clock.now())
        for zone_id in recovered:
            self._open_valve(zone_id, "zone_auto_recovered")
        return recovered

    def guard(self, zone_id: str) -> None:
        """操作前按物理分区做泄漏联锁。"""

        self._zones.guard(zone_id)

    def reset_all(self) -> None:
        """交付前复位：清空各分区与隔离阀运行态。"""

        for zone_id in self.ids():
            self._zones.zone(zone_id).hard_reset()
            self._valves[zone_id].hard_reset()

    def total_samples(self) -> int:
        return self._zones.total_samples()

    def isolated_zones(self) -> list[str]:
        return self._zones.isolated_zones()

    def offline_zones(self) -> list[str]:
        return self._zones.offline_zones()

    def any_blocked(self) -> bool:
        return self._zones.any_blocked()

    def valve_snapshot(self, zone_id: str | None = None) -> dict:
        """返回隔离阀阀位快照（单区或全部）。"""

        if zone_id is not None:
            return self._valves[zone_id].snapshot()
        return {item: valve.snapshot() for item, valve in self._valves.items()}

    def summary(self) -> dict:
        """汇总各物理分区状态（含隔离阀阀位），供控制台与审计读取。"""

        report = self._zones.summary(self._clock.now())
        for zone in report["zones"]:
            zone["valve"] = self._valves[zone["zone"]].snapshot()
        report["valves"] = self.valve_snapshot()
        report["default_zone"] = DEFAULT_LEAK_ZONE
        return report
