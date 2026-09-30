"""一条命令启动：初始化 SQLite、生成虚构票据/照片、载入全部样例（含重复票/混放/部分召回），
启动本地 Web 服务与进程内扫描任务。

    python3 -m poultry_system.run [--host 127.0.0.1] [--port 8000] [--reset] [--interval 30]

数据：./market_data/poultry.db；虚构票据/照片：./market_data/tickets|photos
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import db as dbmod
from .adapters import ScaleAdapter, ThermoAdapter
from .service import MarketService
from .seed import seed
from .server import App, serve
from .tasks import ScanScheduler


def main(argv=None):
    ap = argparse.ArgumentParser(description="禽肉检疫票与分割批次系统（虚构演示）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true", help="删除既有库并重新载入样例")
    ap.add_argument("--interval", type=float, default=30.0, help="扫描间隔秒")
    ap.add_argument("--data-dir", default="market_data")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "poultry.db"
    if args.reset and db_path.exists():
        for p in (db_path, db_path.with_suffix(".db-wal"), db_path.with_suffix(".db-shm")):
            if p.exists():
                p.unlink()

    fresh = not db_path.exists()
    conn = dbmod.connect(db_path)
    dbmod.init_db(conn)

    if fresh:
        print("[seed] 首次启动，载入虚构样例（正常链/重复票/混放/部分召回/到期/超温/标签脱落/异常废弃）…")
        summary = seed(conn, data_dir)
        print("[seed] 批次重量守恒：", summary["conservation"])
        print("[seed] 扫描结果：",
              "到期", len(summary["scan"]["ticket_expiry"]),
              "超温", len(summary["scan"]["overtemp"]))
    else:
        print("[seed] 复用既有数据库（如需重置样例：--reset）")

    scale, thermo = ScaleAdapter("precise"), ThermoAdapter()
    app = App(db_path, scale=scale, thermo=thermo, data_dir=data_dir)

    # 扫描任务使用独立连接（运行于后台线程）
    scan_conn = dbmod.connect(db_path)
    scan_svc = MarketService(scan_conn, scale=scale, thermo=thermo)

    def on_actions(kind, actions):
        print(f"[scan] {kind}: {actions}")

    scheduler = ScanScheduler(scan_svc, interval_s=args.interval, on_actions=on_actions)
    app.scheduler = scheduler
    scheduler.start()

    httpd = serve(app, args.host, args.port)
    url = f"http://{args.host}:{args.port}/"
    print("=" * 64)
    print("禽肉检疫票与分割批次系统已启动（所有供应商/票据/照片均为虚构）")
    print(f"  页面：{url}")
    print("  演示账号：admin（市场管理员）/ zhang,li,wang（摊主）")
    print("  数据：", db_path)
    print("  Ctrl+C 退出")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        scheduler.stop()
        httpd.server_close()
        app.close()
        scan_conn.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
