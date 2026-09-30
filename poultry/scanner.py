"""进程内定时扫描：票据到期、冷藏超温、冻结范围巡检。"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from . import config


class ScanWorker(threading.Thread):
    def __init__(self, svc, interval: float = config.SCAN_INTERVAL,
                 logger: Optional[Callable[[str], None]] = None):
        super().__init__(daemon=True, name="poultry-scan")
        self.svc = svc
        self.interval = interval
        self._stop = threading.Event()
        self._log = logger or (lambda m: None)
        self.last_result: dict = {}

    def run_once(self) -> dict:
        try:
            r1 = self.svc.scan_cert_expiry()
            r2 = self.svc.scan_temps()
            r3 = self.svc.scan_freeze_scope()
            result = {"expiry": r1, "temp": r2, "freeze_scope": r3}
            self.last_result = result
            if r1["expired"] or r2["alerts"]:
                self._log(f"[scan] 到期票 {len(r1['expired'])} 张，"
                          f"超温格位 {len(r2['alerts'])} 个")
            return result
        except Exception as e:  # 扫描线程不应崩溃
            self._log(f"[scan] 扫描异常：{e}")
            return {"error": str(e)}

    def run(self) -> None:
        # 启动后先扫一次，之后按间隔
        self.run_once()
        while not self._stop.wait(self.interval):
            self.run_once()

    def stop(self) -> None:
        self._stop.set()
