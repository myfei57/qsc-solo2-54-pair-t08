"""物理分区隔离阀。

每个物理分区一只隔离阀，作为泄漏隔离动作的物理执行端：分区状态机决定
"何时隔离"，隔离阀落实"关阀/恢复"。阀位只在 ``open``/``closed`` 间切换，
对已经处于目标阀位的动作直接拒绝，避免把重复关阀误记成一次新的隔离。
"""

from __future__ import annotations

import threading

from ..errors import InterlockError

OPEN = "open"
CLOSED = "closed"


class LeakIsolationValve:
    """单个物理分区的泄漏隔离阀。"""

    def __init__(self, zone_id: str, label: str = "") -> None:
        self._zone_id = zone_id
        self._label = label or zone_id
        self._position = OPEN
        self._closures = 0
        self._reopens = 0
        self._lock = threading.RLock()

    @property
    def zone_id(self) -> str:
        return self._zone_id

    @property
    def label(self) -> str:
        return self._label

    def is_open(self) -> bool:
        return self._position == OPEN

    def close(self) -> bool:
        """关阀隔离，返回本次是否真的发生了关阀动作。"""

        with self._lock:
            if self._position == CLOSED:
                raise InterlockError(
                    f"分区 {self._zone_id} 隔离阀已处于关闭位，无需重复隔离",
                    component="leak",
                )
            self._position = CLOSED
            self._closures += 1
            return True

    def reopen(self) -> bool:
        """恢复后重新开阀，返回本次是否真的发生了开阀动作。"""

        with self._lock:
            if self._position == OPEN:
                raise InterlockError(
                    f"分区 {self._zone_id} 隔离阀已处于打开位，无需重复恢复",
                    component="leak",
                )
            self._position = OPEN
            self._reopens += 1
            return True

    def closures(self) -> int:
        return self._closures

    def reopens(self) -> int:
        return self._reopens

    def hard_reset(self) -> None:
        """交付前复位：阀回打开位并清零动作计数。"""

        with self._lock:
            self._position = OPEN
            self._closures = 0
            self._reopens = 0

    def snapshot(self) -> dict:
        return {
            "zone": self._zone_id,
            "label": self._label,
            "position": self._position,
            "closed": self._position == CLOSED,
            "closures": self._closures,
            "reopens": self._reopens,
        }
