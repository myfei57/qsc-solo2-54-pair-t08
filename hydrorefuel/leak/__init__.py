"""分物理区泄漏检测组件。

泄漏检测按**物理分区**多路布置（不按管路分支）：每区一个探头、一只隔离阀。
哪一路浓度超阀值或探头掉线，就只隔离那一路，其他分区照常运行；多路并发超
标时各自带来源，汇总侧区分单点与多点。隔离后泄漏没消干净（含探头掉线）绝
不自动恢复；泄漏消除后是自动恢复还是必须人工复位，按区配置。所有隔离、
恢复、上下线与复位动作都经事件总线落审计。
"""

from __future__ import annotations

from .controller import DEFAULT_LEAK_ZONE, LeakZoneController
from .snapshot import LeakZoneSnapshot, LeakZonesSnapshot
from .valve import CLOSED, OPEN, LeakIsolationValve
from .zone import AUTO, MANUAL, LeakZone
from .zones import LeakZones

__all__ = [
    "AUTO",
    "CLOSED",
    "DEFAULT_LEAK_ZONE",
    "MANUAL",
    "OPEN",
    "LeakIsolationValve",
    "LeakZone",
    "LeakZoneController",
    "LeakZoneSnapshot",
    "LeakZones",
    "LeakZonesSnapshot",
]
