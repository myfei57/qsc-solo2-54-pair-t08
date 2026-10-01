"""多分区泄漏检测面板。

面板把全站泄漏检测按**管路分支**拆成若干分区（分区划分定死为分支粒度，
物理分区只做控制台展示分组），每个分区独立检测、独立隔离：哪一路超阀值
就只隔离哪一路，其他分支继续运行。

设计上有三条硬约束：

1. **迁移收口**：隔离、自动恢复、人工复位全部经由面板内部同一组迁移函数
   完成，每次迁移当场向事件总线发布一条事件；审计日志是总线的全量订阅者，
   因此隔离动作、复位与审计记录天然一一对应，不会出现"动作发生了但没
   落账"。
2. **并发可区分**：同一关联窗口内触发的多个分区归入同一个泄漏事件
   （incident），事件携带 ``single`` / ``multi`` 类别与来源分区清单，
   控制台与审计都能分清是单点还是多点同时超标。
3. **掉线按泄漏处理**：探头掉线属于检测系统的部分失败，对应分区立即按
   有泄漏隔离，面板置 ``degraded``；探头恢复且读数回落前，该分区不允许
   任何恢复。
"""

from __future__ import annotations

import threading

from ..clock import Clock
from ..config import LeakLimits
from ..errors import LeakError
from ..eventbus import Event, EventBus
from .zone import LeakZoneMonitor

_RECENT_INCIDENTS = 8


class LeakPanel:
    """按管路分支划分的多分区泄漏检测面板。"""

    def __init__(self, limits: LeakLimits, clock: Clock, bus: EventBus, station: str = "") -> None:
        self._limits = limits
        self._clock = clock
        self._bus = bus
        self._station = station
        self._zones = {spec.name: LeakZoneMonitor(spec, limits) for spec in limits.zones}
        self._primary = limits.zones[0].name
        self._incidents: list[dict] = []
        self._incident_seq = 0
        self._zone_incidents: dict[str, str] = {}
        self._lock = threading.RLock()

    @property
    def primary_name(self) -> str:
        """主分区名：兼容单探头时代的采样／复位入口。"""

        return self._primary

    @property
    def primary(self) -> LeakZoneMonitor:
        return self._zones[self._primary]

    def zone(self, name: str) -> LeakZoneMonitor:
        """按名字取分区监控器，未知分区当场报错。"""

        try:
            return self._zones[name]
        except KeyError as error:
            raise LeakError(f"未知泄漏分区: {name}", component="leak") from error

    def sample(self, zone_name: str, concentration_ppm: float | None, probe_ok: bool = True) -> dict:
        """记一次分区采样；超阀值或探头掉线即隔离该分区，返回分区最新状态。"""

        with self._lock:
            zone = self.zone(zone_name)
            if probe_ok and concentration_ppm is None:
                raise LeakError(f"分区 {zone_name} 探头在线时采样必须给出浓度读数", component="leak")
            spec = zone.spec
            now = self._clock.now()
            was_probe_ok = zone.probe_ok
            outcome = zone.sample_reading(now, concentration_ppm, probe_ok=probe_ok)
            self._publish(
                "sample",
                severity="alarm" if outcome["tripped"] else "notice",
                zone=spec.name,
                branch=spec.branch,
                ppm=None if concentration_ppm is None else round(float(concentration_ppm), 2),
                probe_ok=zone.probe_ok,
            )
            if was_probe_ok and not zone.probe_ok:
                self._publish(
                    "probe_offline",
                    severity="warning",
                    zone=spec.name,
                    branch=spec.branch,
                    message=f"分区 {spec.name} 探头掉线（部分失败），按有泄漏处理",
                )
            elif not was_probe_ok and zone.probe_ok:
                self._publish(
                    "probe_recovered",
                    severity="notice",
                    zone=spec.name,
                    branch=spec.branch,
                    message=f"分区 {spec.name} 探头恢复在线",
                )
            if outcome["transition"] == "isolated":
                incident_id, became_multi = self._join_incident(spec.name, now)
                self._zone_incidents[spec.name] = incident_id
                incident = self._find_incident(incident_id)
                self._publish(
                    "zone_isolated",
                    severity="alarm",
                    zone=spec.name,
                    branch=spec.branch,
                    area=spec.area,
                    cause=outcome["cause"],
                    incident=incident_id,
                    incident_kind=self._kind(incident),
                    message=f"分区 {spec.name} 泄漏（{outcome['cause']}），已隔离该分支",
                )
                if became_multi:
                    self._publish(
                        "incident_escalated",
                        severity="alarm",
                        incident=incident_id,
                        zones=list(incident["zones"]),
                        message=f"泄漏事件 {incident_id} 升级为多点同时超标：{incident['zones']}",
                    )
            elif outcome["transition"] == "recovered":
                self._after_recovery(zone, now, trigger="auto", actor="system")
            snapshot = zone.snapshot(now, incident=self._zone_incidents.get(spec.name, "")).as_dict()
            snapshot["transition"] = outcome["transition"]
            return snapshot

    def reset_zone(self, zone_name: str, actor: str = "console") -> dict:
        """人工复位一个已隔离的分区；泄漏未消除或保持期未走完会被拒绝。"""

        with self._lock:
            zone = self.zone(zone_name)
            now = self._clock.now()
            released = zone.reset(now)
            if released:
                self._after_recovery(zone, now, trigger="manual", actor=actor)
            return {
                "zone": zone.spec.name,
                "released": released,
                "state": zone.state,
                "cycles": zone.latch.cycles(),
            }

    def reset(self) -> dict:
        """交付前重新标定：全部分区清态、隔离阀复位、未结事件收尾。"""

        with self._lock:
            now = self._clock.now()
            for zone in self._zones.values():
                zone.clear()
            for incident in self._incidents:
                if incident["closed_ts"] is None:
                    incident["closed_ts"] = now
            self._zone_incidents.clear()
            return {"zones_cleared": len(self._zones)}

    def any_isolated(self) -> bool:
        """是否有任一分区处于隔离态（加注就绪判据用）。"""

        return any(zone.isolated for zone in self._zones.values())

    def isolated_zones(self) -> list[str]:
        """返回当前被隔离的分区名列表。"""

        return [name for name, zone in self._zones.items() if zone.isolated]

    def degraded(self) -> bool:
        """检测系统是否部分失败（有探头掉线）。"""

        return any(not zone.probe_ok for zone in self._zones.values())

    def snapshot(self) -> dict:
        """返回全部分区状态与事件关联，供控制台展示。"""

        with self._lock:
            now = self._clock.now()
            zones = [
                zone.snapshot(now, incident=self._zone_incidents.get(name, "")).as_dict()
                for name, zone in self._zones.items()
            ]
            return {
                "zones": zones,
                "isolated_zones": self.isolated_zones(),
                "degraded": self.degraded(),
                "incidents": self.incident_view(),
            }

    def incident_view(self) -> dict:
        """返回泄漏事件视图：进行中的事件与最近已结事件。"""

        with self._lock:
            active = [self._incident_dict(item) for item in self._incidents if item["closed_ts"] is None]
            closed = [self._incident_dict(item) for item in self._incidents if item["closed_ts"] is not None]
            return {
                "active": active,
                "recent_closed": list(reversed(closed[-_RECENT_INCIDENTS:])),
                "total": self._incident_seq,
            }

    def _join_incident(self, zone_name: str, now: float) -> tuple[str, bool]:
        """把一次隔离归入泄漏事件：窗口内归入最近事件，否则开新事件。

        返回事件 id 与这次归并是否把事件升级成了多点。
        """

        for incident in reversed(self._incidents):
            if incident["closed_ts"] is not None:
                continue
            if now - incident["last_trip_ts"] <= self._limits.incident_window_s:
                became_multi = False
                if zone_name not in incident["zones"]:
                    incident["zones"].append(zone_name)
                    became_multi = len(incident["zones"]) == 2
                incident["last_trip_ts"] = now
                return incident["id"], became_multi
        self._incident_seq += 1
        self._incidents.append(
            {
                "id": f"inc-{self._incident_seq}",
                "zones": [zone_name],
                "started_ts": now,
                "last_trip_ts": now,
                "closed_ts": None,
            }
        )
        return f"inc-{self._incident_seq}", False

    def _after_recovery(self, zone: LeakZoneMonitor, now: float, trigger: str, actor: str) -> None:
        """恢复后的收口：解除事件关联、必要时结案、落审计。"""

        spec = zone.spec
        incident_id = self._zone_incidents.pop(spec.name, "")
        if incident_id:
            incident = self._find_incident(incident_id)
            if incident is not None and not any(
                self._zones[name].isolated for name in incident["zones"] if name in self._zones
            ):
                incident["closed_ts"] = now
        self._publish(
            "zone_recovered",
            severity="notice",
            zone=spec.name,
            branch=spec.branch,
            trigger=trigger,
            actor=actor,
            incident=incident_id,
            message=f"分区 {spec.name} 已恢复（{trigger}，{actor}）",
        )

    def _find_incident(self, incident_id: str) -> dict | None:
        for incident in reversed(self._incidents):
            if incident["id"] == incident_id:
                return incident
        return None

    def _kind(self, incident: dict | None) -> str:
        if incident is None:
            return "single"
        return "multi" if len(incident["zones"]) > 1 else "single"

    def _incident_dict(self, incident: dict) -> dict:
        return {
            "id": incident["id"],
            "kind": self._kind(incident),
            "zones": list(incident["zones"]),
            "started_ts": round(incident["started_ts"], 3),
            "last_trip_ts": round(incident["last_trip_ts"], 3),
            "closed_ts": None if incident["closed_ts"] is None else round(incident["closed_ts"], 3),
            "active": incident["closed_ts"] is None,
        }

    def _publish(self, action: str, severity: str = "notice", **payload: object) -> None:
        self._bus.publish(
            Event.of("leak", "leak", action, station=self._station, severity=severity, **payload)
        )
