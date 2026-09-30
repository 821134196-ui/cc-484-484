"""模拟适配器：电子秤与冷藏温度探头。

真实环境对接驱动；这里用可注入的偏差脚本返回**回执**（ref_no + 读数），
业务层只信回执并据此判定称重差异 / 超温。无外部依赖、可离线、可重放。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Callable, Dict, List, Optional


class ScaleAdapter:
    """模拟电子秤。

    模式
    ----
    * precise：读数=申报值。
    * scripted：按预置脚本（key -> 偏差克数或读数）逐次返回，用于制造称重差异。
    """

    def __init__(self, mode: str = "precise",
                 scripts: Optional[Dict[str, List[int]]] = None,
                 default_drift_g: int = 0,
                 now_func: Callable[[], datetime] = datetime.now):
        assert mode in ("precise", "scripted")
        self.mode = mode
        self.scripts = scripts or {}          # key -> [drift_g, ...]
        self._cursor: Dict[str, int] = {}
        self.default_drift_g = default_drift_g
        self.now = now_func
        self._n = 0

    def _drift(self, key: str) -> int:
        if self.mode != "scripted":
            return self.default_drift_g
        seq = self.scripts.get(key)
        if not seq:
            return self.default_drift_g
        i = self._cursor.get(key, 0)
        drift = seq[min(i, len(seq) - 1)]
        self._cursor[key] = i + 1
        return drift

    def _receipt(self, kind: str, key: str, declared_g: int, measured_g: int) -> dict:
        self._n += 1
        return {
            "ref_no": f"SCALE-{self.now():%Y%m%d}-{self._n:04d}-{uuid.uuid4().hex[:8]}",
            "device": "MOCK-SCALE-01",
            "kind": kind,
            "ref_key": key,
            "declared_g": declared_g,
            "measured_g": measured_g,
            "stable": True,
            "measured_at": self.now().isoformat(timespec="seconds"),
        }

    def weigh_arrival(self, ticket_no: str, declared_g: int) -> dict:
        drift = self._drift(ticket_no)
        return self._receipt("arrival", ticket_no, declared_g, declared_g + drift)

    def weigh_cut(self, batch_no: str, declared_g: int) -> dict:
        drift = self._drift(batch_no)
        return self._receipt("cut", batch_no, declared_g, declared_g + drift)


class ThermoAdapter:
    """模拟冷藏温度探头：为每次读数生成回执。"""

    def __init__(self, device_map: Optional[Dict[str, str]] = None,
                 now_func: Callable[[], datetime] = datetime.now):
        self.devices = device_map or {}
        self.now = now_func
        self._n = 0

    def last_receipt(self, cabinet_code: str, temp_c: float) -> dict:
        self._n += 1
        return {
            "ref_no": f"THERMO-{self.now():%Y%m%d}-{self._n:04d}-{uuid.uuid4().hex[:8]}",
            "device": self.devices.get(cabinet_code, "MOCK-PROBE-01"),
            "cabinet": cabinet_code,
            "temp_c": temp_c,
            "recorded_at": self.now().isoformat(timespec="seconds"),
        }
