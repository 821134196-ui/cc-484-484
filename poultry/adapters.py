"""模拟硬件适配器：电子秤与冷藏温度探头。

真实部署时替换为串口/HTTP 驱动即可，这里返回带回执号的模拟读数，
并支持预置脚本（偏差、温度异常）以驱动异常处理流程。
"""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass
from typing import Optional

from . import config


@dataclass(frozen=True)
class ScaleReceipt:
    receipt_no: str          # 称重回执号
    weight: float            # kg
    stable: bool             # 读数是否稳定
    device: str


@dataclass(frozen=True)
class TempReceipt:
    receipt_no: str
    cell_code: str
    temp: float
    ok: bool
    device: str


class MockScale:
    """电子秤模拟器。"""

    def __init__(self, device: str = "SCALE-01") -> None:
        self.device = device
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        # 预置：下一次称重的强制读数（用于称重差异场景）
        self._next_override: Optional[float] = None
        self._next_unstable = False

    def preset_next(self, weight: Optional[float] = None, *, unstable: bool = False) -> None:
        self._next_override = weight
        self._next_unstable = unstable

    def weigh(self, expected: Optional[float] = None, *, stable: bool = True) -> ScaleReceipt:
        with self._lock:
            n = next(self._seq)
            unstable = self._next_unstable or not stable
            self._next_unstable = False
            if self._next_override is not None:
                w = self._next_override
                self._next_override = None
            elif expected is not None:
                # 正常情况：与预期相差 ±0.2% 以内
                w = round(expected * (1 + ((n % 3) - 1) * 0.0008), 3)
                if w <= 0:
                    w = expected
            else:
                w = 0.0
            return ScaleReceipt(receipt_no=f"W{n:06d}",
                                weight=round(w, 3), stable=not unstable,
                                device=self.device)


class MockTempProbe:
    """冷藏温度探头模拟器。

    forced_temps: {格位编码: 强制温度}，用于冷藏超温场景；
    默认在合规区间小幅波动。
    """

    def __init__(self, device: str = "PROBE-01") -> None:
        self.device = device
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self.forced: dict[str, float] = {}

    def force_temp(self, cell_code: str, temp: float) -> None:
        self.forced[cell_code] = temp

    def clear_force(self, cell_code: str) -> None:
        self.forced.pop(cell_code, None)

    def read(self, cell_code: str) -> TempReceipt:
        with self._lock:
            n = next(self._seq)
            if cell_code in self.forced:
                t = self.forced[cell_code]
            else:
                # 确定性波动：-2.4 ~ 2.4
                t = round((((n * 7) % 49) - 24) / 10.0, 1)
            ok = config.COLD_TEMP_MIN <= t <= config.COLD_TEMP_MAX
            return TempReceipt(receipt_no=f"T{n:06d}", cell_code=cell_code,
                               temp=t, ok=ok, device=self.device)
