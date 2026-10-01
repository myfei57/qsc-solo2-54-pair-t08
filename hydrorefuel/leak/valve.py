"""分区隔离阀。

每条管路分支一台隔离阀，只有"开"与"关"两态：关阀即把该分支从工艺链上
隔离，其他分支不受影响继续运行。冗余的开／关调用直接报错，避免审计里出现
"没隔离却恢复了"的假记录。
"""

from __future__ import annotations

from ..errors import InterlockError


class IsolationValve:
    """管路分支隔离阀。"""

    def __init__(self, branch: str) -> None:
        self._branch = branch
        self._open = True
        self._isolations = 0

    @property
    def branch(self) -> str:
        return self._branch

    def close(self) -> None:
        """关闭隔离阀，把分支从工艺链上隔离开。"""

        if not self._open:
            raise InterlockError(f"分支 {self._branch} 隔离阀已关闭", component="leak")
        self._open = False
        self._isolations += 1

    def open(self) -> None:
        """重新打开隔离阀，恢复该分支运行。"""

        if self._open:
            raise InterlockError(f"分支 {self._branch} 隔离阀未隔离，无需恢复", component="leak")
        self._open = True

    def is_open(self) -> bool:
        """隔离阀是否处于打开状态。"""

        return self._open

    def isolations(self) -> int:
        """返回累计隔离次数。"""

        return self._isolations
