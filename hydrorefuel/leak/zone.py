"""按物理分区的泄漏探测区状态机。

分区口径在系统里定死为**物理分区**（防火/工艺物理隔断），不按管路分支：
一个物理区里可能横跨多根管路分支，泄漏时把整区隔离，同一物理区不再按分支
拆分隔离动作。每个物理区配备一个探头与一个隔离阀，状态机只管这一区的采样
判定、隔离保持与恢复。

恢复策略按区配置：

* ``manual``（人工复位）：隔离后即使浓度回落、探头恢复，也必须由人工显式
  复位；保持期未满或泄漏仍未消干净时，复位一律拒绝；
* ``auto``（自动恢复）：保持期走完且泄漏消干净后自动复位；只要泄漏没消
  干净，就一直隔离，绝不自动恢复。

探头掉线属于**部分失败**，按有泄漏处理：掉线即隔离并按泄漏闩锁保持，直到
探头重新上线并给出一次低于阀值的采样，才谈得上恢复。
"""

from __future__ import annotations

import threading

from ..errors import LeakError

ONLINE = "online"
OFFLINE = "offline"

MANUAL = "manual"
AUTO = "auto"

RECOVERY_MODES = (MANUAL, AUTO)


class LeakZone:
    """一个物理分区的探头采样、隔离与闩锁状态机。"""

    def __init__(
        self,
        zone_id: str,
        label: str,
        threshold_ppm: float,
        hold_s: float,
        recovery: str = MANUAL,
    ) -> None:
        if not zone_id:
            raise ValueError("分区标识不能为空")
        if recovery not in RECOVERY_MODES:
            raise ValueError(f"恢复策略必须是 {RECOVERY_MODES} 之一")
        if hold_s <= 0:
            raise ValueError("保持时长必须为正")
        if threshold_ppm <= 0:
            raise ValueError("泄漏阀值必须为正")
        self._id = zone_id
        self._label = label
        self._threshold_ppm = float(threshold_ppm)
        self._hold_s = float(hold_s)
        self._recovery = recovery
        self._lock = threading.RLock()
        self._online = True
        self._isolated = False
        self._latched = False
        self._until = 0.0
        self._ppm: float | None = 0.0
        self._peak_ppm = 0.0
        self._samples = 0
        self._trips = 0
        self._resets = 0
        self._recovered = 0
        self._trip_sources: tuple[str, ...] = ()
        self._last_reason = ""

    @property
    def zone_id(self) -> str:
        return self._id

    @property
    def label(self) -> str:
        return self._label

    @property
    def threshold_ppm(self) -> float:
        return self._threshold_ppm

    @property
    def hold_s(self) -> float:
        return self._hold_s

    @property
    def recovery(self) -> str:
        return self._recovery

    def online(self) -> bool:
        """探头是否在线。"""

        return self._online

    def isolated(self) -> bool:
        """本区是否处于隔离（管路切断）状态。"""

        return self._isolated

    def tripped(self) -> bool:
        """闩锁是否仍挂着（隔离保持中，尚未恢复）。"""

        return self._latched

    def peak_ppm(self) -> float:
        return self._peak_ppm

    def samples(self) -> int:
        return self._samples

    def trips(self) -> int:
        return self._trips

    def resets(self) -> int:
        return self._resets

    def auto_recoveries(self) -> int:
        return self._recovered

    def remaining_s(self, now: float) -> float:
        with self._lock:
            if not self._latched:
                return 0.0
            return max(0.0, self._until - float(now))

    def is_leaking(self) -> bool:
        """当前是否仍有泄漏：探头掉线或尚无新采样，按有泄漏处理。"""

        with self._lock:
            if not self._latched:
                return False
            if not self._online or self._ppm is None:
                return True
            return self._ppm >= self._threshold_ppm

    def _engage(self, now: float, reason: str, sources: tuple[str, ...]) -> bool:
        """触发隔离并咬合闩锁，返回是否为一次新的隔离事件。"""

        started = not self._latched
        if started:
            # 新一轮隔离事件重新计峰值与来源，避免把上一轮已恢复的旧峰值
            # 带进本次归因。探头掉线没有浓度读数，峰值记 0。
            self._peak_ppm = self._ppm or 0.0
            self._trips += 1
            self._trip_sources = ()
        self._latched = True
        self._isolated = True
        self._until = float(now) + self._hold_s
        # 每一次确认到的超标来源都并入本次事件来源集合，便于多点归因。
        merged = [item for item in self._trip_sources if item not in sources]
        self._trip_sources = tuple(merged) + sources
        self._last_reason = reason
        return started

    def sample(self, now: float, concentration_ppm: float, sources: tuple[str, ...] = ()) -> dict:
        """记一次在线浓度采样；达到阀值即隔离并咬合闩锁。

        隔离期间再次采到超标读数会重新计保持期，保证"最近一次超标到恢复"
        至少隔满一个保持期。浓度回落本身不解除隔离：自动恢复区也必须等
        保持期走完后由控制周期（:meth:`tick`）统一开阀，保证"消干净后自动
        恢复"与"控制周期驱动"两个口径一致。
        """

        with self._lock:
            value = float(concentration_ppm)
            self._online = True
            self._samples += 1
            self._ppm = value
            self._peak_ppm = max(self._peak_ppm, value)
            tripped_now = value >= self._threshold_ppm
            engaged_now = False
            if tripped_now:
                engaged_now = self._engage(now, "leak", sources or ("concentration",))
                self._until = float(now) + self._hold_s
            return {
                "zone": self._id,
                "ppm": round(value, 2),
                "threshold_ppm": self._threshold_ppm,
                "tripped": tripped_now,
                "isolated": self._isolated,
                "latched": self._latched,
                "new_isolation": engaged_now,
                "auto_recovered": False,
                "remaining_s": round(self.remaining_s(now), 1),
            }

    def mark_offline(self, now: float) -> dict:
        """探头掉线：部分失败按有泄漏处理，立即隔离并咬合闩锁。"""

        with self._lock:
            already = not self._online
            self._online = False
            # 掉线后旧读数不再可信，清空为"尚无有效采样"。
            self._ppm = None
            isolated_now = self._engage(now, "probe_offline", ("probe_offline",))
            return {
                "zone": self._id,
                "online": False,
                "already_offline": already,
                "isolated": self._isolated,
                "latched": self._latched,
                "new_isolation": isolated_now,
                "remaining_s": round(self.remaining_s(now), 1),
            }

    def mark_online(self, now: float) -> dict:
        """探头重新上线；是否能恢复由后续干净采样与控制周期/人工复位决定。"""

        with self._lock:
            was_offline = not self._online
            self._online = True
            return {
                "zone": self._id,
                "online": True,
                "was_offline": was_offline,
                "isolated": self._isolated,
                "latched": self._latched,
                "auto_recovered": False,
                "remaining_s": round(self.remaining_s(now), 1),
            }

    def _hold_elapsed(self, now: float) -> bool:
        return self._latched and float(now) >= self._until

    def _maybe_auto_recover(self, now: float) -> bool:
        """自动恢复分区：仅自动复位区、保持期走完且泄漏消干净才动作。"""

        if not self._latched or self._recovery != AUTO:
            return False
        if not self._hold_elapsed(now) or self.is_leaking():
            return False
        self._recover(now)
        self._recovered += 1
        return True

    def _recover(self, now: float) -> None:
        self._latched = False
        self._isolated = False
        self._until = 0.0
        self._trip_sources = ()
        self._last_reason = ""
        self._peak_ppm = 0.0
        self._ppm = 0.0

    def manual_reset(self, now: float) -> dict:
        """人工复位。

        保持期未满、泄漏未消干净（含探头掉线）时一律拒绝，杜绝带泄漏自动或
        人工带病恢复。恢复策略为自动的分区也允许人工提前确认（同样要满足
        上面条件）。
        """

        with self._lock:
            if not self._latched:
                return {"zone": self._id, "reset": False, "already_clear": True}
            if not self._hold_elapsed(now):
                raise LeakError(
                    f"分区 {self._id} 泄漏隔离保持期还差 {self.remaining_s(now):.1f} 秒，不能复位",
                    component="leak",
                )
            if not self._online:
                raise LeakError(
                    f"分区 {self._id} 探头掉线，按有泄漏处理，不能复位",
                    component="leak",
                )
            if self._ppm is None:
                raise LeakError(
                    f"分区 {self._id} 探头恢复后尚无低于阀值的采样，泄漏未消干净，不能复位",
                    component="leak",
                )
            if self._ppm >= self._threshold_ppm:
                raise LeakError(
                    f"分区 {self._id} 浓度 {self._ppm:.1f} 仍高于阀值 "
                    f"{self._threshold_ppm:.1f}，泄漏未消干净，不能复位",
                    component="leak",
                )
            self._recover(now)
            self._resets += 1
            return {"zone": self._id, "reset": True, "already_clear": False}

    def tick(self, now: float) -> bool:
        """推进时间：驱动自动恢复区在泄漏消干净后自动恢复。"""

        with self._lock:
            return self._maybe_auto_recover(now)

    def hard_reset(self) -> None:
        """交付前复位：清掉本区全部运行态与累计计数。"""

        with self._lock:
            self._online = True
            self._isolated = False
            self._latched = False
            self._until = 0.0
            self._ppm = 0.0
            self._peak_ppm = 0.0
            self._samples = 0
            self._trips = 0
            self._resets = 0
            self._recovered = 0
            self._trip_sources = ()
            self._last_reason = ""

    def snapshot(self, now: float) -> dict:
        """返回本区状态快照。"""

        with self._lock:
            leaking = self.is_leaking()
            return {
                "zone": self._id,
                "label": self._label,
                "recovery": self._recovery,
                "probe_online": self._online,
                "ppm": round(self._ppm, 2) if self._ppm is not None else None,
                "peak_ppm": round(self._peak_ppm, 2),
                "threshold_ppm": round(self._threshold_ppm, 2),
                "samples": self._samples,
                "tripped": self._latched,
                "isolated": self._isolated,
                "leaking": leaking,
                "hold_remaining_s": round(self.remaining_s(now), 1),
                "hold_elapsed": self._hold_elapsed(now),
                "sources": list(self._trip_sources),
                "trip_reason": self._last_reason,
                "trips": self._trips,
                "manual_resets": self._resets,
                "auto_recoveries": self._recovered,
                "ready": not self._isolated and self._online,
            }
