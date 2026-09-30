"""进程内任务：周期性扫描检疫票到期与冷藏超温，触发冻结。

单进程守护线程；扫描动作通过 service 的 BEGIN IMMEDIATE 事务串行执行，
与分割/销售等写操作互斥。提供 run_once() 便于测试与手动触发。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional

from .service import MarketService


class ScanScheduler:
    def __init__(self, svc: MarketService, interval_s: float = 30.0,
                 on_actions: Optional[Callable[[str, list], None]] = None):
        self.svc = svc
        self.interval_s = interval_s
        self.on_actions = on_actions or (lambda kind, actions: None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def run_once(self) -> dict:
        """执行一轮扫描（加锁，避免与定时线程重入）。"""
        with self._lock:
            expiry = self.svc.scan_ticket_expiry()
            overtemp = self.svc.scan_overtemp()
            if expiry:
                self.on_actions("ticket_expiry", expiry)
            if overtemp:
                self.on_actions("overtemp", overtemp)
            return {"ticket_expiry": expiry, "overtemp": overtemp}

    def _loop(self):
        while not self._stop.wait(self.interval_s):
            try:
                self.run_once()
            except Exception as exc:  # 扫描异常不应杀死进程
                self.on_actions("error", [{"error": str(exc)}])

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="market-scan",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
