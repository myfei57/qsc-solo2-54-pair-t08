"""单分区泄漏监控器。

每个分区监控器守一条管路分支：一支探头（:class:`LeakDetector`）、一只闩锁
（:class:`LeakLatch`）、一台隔离阀（:class:`IsolationValve`）。状态机只有
两个稳态：

``clear``     读数低于阀值，隔离阀打开，分支正常运行；
``isolated``  超阀值或探头掉线，闩锁咬合、隔离阀关闭，分支被隔离。

恢复路径按分区的 ``recovery`` 策略分流：``auto`` 分区在泄漏消除且闩锁保持
期走完后，随下一次合格采样自动恢复；``manual`` 分区必须等人工复位。无论
哪种策略，只要泄漏没消干净（当前读数仍超阀值，或探头仍离线——掉线按有
泄漏处理），任何恢复都不允许发生。
"""

from __future__ import annotations

from ..config import LeakLimits, LeakZoneSpec
from ..errors import LeakError
from .detector import LeakDetector
from .latch import LeakLatch
from .snapshot import ZoneSnapshot
from .valve import IsolationValve

CAUSE_THRESHOLD = "threshold"
CAUSE_OFFLINE = "offline"


class LeakZoneMonitor:
    """一条管路分支的泄漏检测、隔离与恢复。"""

    def __init__(self, spec: LeakZoneSpec, limits: LeakLimits) -> None:
        self._spec = spec
        self._detector = LeakDetector(limits)
        self._latch = LeakLatch(limits.hold_s)
        self._valve = IsolationValve(spec.branch)
        self._isolated = False
        self._cause = ""
        self._isolated_at = 0.0
        self._last_ppm: float | None = None
        self._probe_ok = True

    @property
    def spec(self) -> LeakZoneSpec:
        return self._spec

    @property
    def detector(self) -> LeakDetector:
        return self._detector

    @property
    def latch(self) -> LeakLatch:
        return self._latch

    @property
    def state(self) -> str:
        return "isolated" if self._isolated else "clear"

    @property
    def isolated(self) -> bool:
        return self._isolated

    @property
    def probe_ok(self) -> bool:
        return self._probe_ok

    def sample_reading(self, now: float, concentration_ppm: float, probe_ok: bool = True) -> dict:
        """记一次采样并推进状态机，返回本次采样引起的迁移。"""

        self._probe_ok = bool(probe_ok)
        outcome = {"tripped": False, "transition": "", "cause": self._cause}
        if not self._probe_ok:
            # 探头掉线属于部分失败，按有泄漏处理：隔离并保持到探头恢复、
            # 读数回落为止。
            self._last_ppm = None
            if not self._isolated:
                self._isolate(now, CAUSE_OFFLINE)
                outcome["transition"] = "isolated"
            outcome["tripped"] = True
            outcome["cause"] = self._cause
            return outcome

        value = float(concentration_ppm)
        self._last_ppm = value
        result = self._detector.sample(value)
        tripped = bool(result["tripped"])
        outcome["tripped"] = tripped
        if tripped:
            if not self._isolated:
                self._isolate(now, CAUSE_THRESHOLD)
                outcome["transition"] = "isolated"
            elif self._cause == CAUSE_OFFLINE:
                # 探头恢复后读数仍超阀值：确认是真实泄漏而非掉线误报。
                self._cause = CAUSE_THRESHOLD
        elif self._isolated and self._spec.recovery == "auto" and self._clearable(now):
            self._recover(now)
            outcome["transition"] = "recovered"
        outcome["cause"] = self._cause
        return outcome

    def reset(self, now: float) -> bool:
        """人工复位：泄漏已消除且保持期走完才允许，返回是否真的复位了。"""

        if not self._isolated:
            return False
        if not self._probe_ok:
            raise LeakError(f"分区 {self._spec.name} 探头离线，按有泄漏处理，不能复位", component="leak")
        if self._last_ppm is None or self._detector.limits.tripped(self._last_ppm):
            raise LeakError(f"分区 {self._spec.name} 泄漏未消除，不能复位", component="leak")
        self._latch.reset(now)
        self._recover(now)
        return True

    def clear(self) -> None:
        """交付前重新标定：清掉检测峰值、闩锁与隔离态，隔离阀重新打开。"""

        self._detector.reset()
        self._latch.force_release()
        if not self._valve.is_open():
            self._valve.open()
        self._isolated = False
        self._cause = ""
        self._isolated_at = 0.0
        self._last_ppm = None
        self._probe_ok = True

    def snapshot(self, now: float, incident: str = "") -> ZoneSnapshot:
        """返回该分区某一时刻的状态快照。"""

        return ZoneSnapshot(
            name=self._spec.name,
            branch=self._valve.branch,
            area=self._spec.area,
            recovery=self._spec.recovery,
            state=self.state,
            cause=self._cause,
            probe_ok=self._probe_ok,
            ppm=self._last_ppm if self._last_ppm is not None else 0.0,
            peak_ppm=self._detector.peak_ppm(),
            threshold_ppm=self._detector.limits.threshold_ppm,
            tripped=self._currently_tripped(),
            latch_engaged=self._latch.is_engaged(now),
            remaining_s=self._latch.remaining_s(now),
            cycles=self._latch.cycles(),
            isolated_at=self._isolated_at,
            incident=incident,
            valve_open=self._valve.is_open(),
            isolations=self._valve.isolations(),
        )

    def _currently_tripped(self) -> bool:
        if not self._probe_ok:
            return True
        return self._last_ppm is not None and self._detector.limits.tripped(self._last_ppm)

    def _clearable(self, now: float) -> bool:
        """泄漏是否已消干净：探头在线、最新读数低于阀值、保持期已走完。"""

        return (
            self._probe_ok
            and self._last_ppm is not None
            and not self._detector.limits.tripped(self._last_ppm)
            and not self._latch.is_engaged(now)
        )

    def _isolate(self, now: float, cause: str) -> None:
        self._valve.close()
        self._latch.engage(now)
        self._isolated = True
        self._cause = cause
        self._isolated_at = float(now)

    def _recover(self, now: float) -> None:
        self._latch.reset(now)
        self._valve.open()
        self._isolated = False
        self._cause = ""
        self._isolated_at = 0.0
