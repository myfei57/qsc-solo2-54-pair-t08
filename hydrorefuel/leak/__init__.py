"""泄漏检测组件。

泄漏检测按管路分支划分分区：每个分区独立采样，达到阀值即触发闩锁并关闭
该分支的隔离阀，其他分支继续运行。探头掉线属于部分失败，按有泄漏处理。
泄漏消除后按分区策略自动恢复或等待人工复位；闩锁保持期内任何恢复都被
拒绝。放散必须先于重新加注，这条约束由闩锁与编排共同保证。
"""

from __future__ import annotations

from .detector import LeakDetector
from .latch import LeakLatch
from .panel import LeakPanel
from .snapshot import LeakSnapshot, ZoneSnapshot
from .valve import IsolationValve
from .zone import LeakZoneMonitor

__all__ = [
    "IsolationValve",
    "LeakDetector",
    "LeakLatch",
    "LeakPanel",
    "LeakSnapshot",
    "LeakZoneMonitor",
    "ZoneSnapshot",
]
