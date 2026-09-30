#!/usr/bin/env python3
"""一条本地启动命令：

    python3 run.py

重置 SQLite、载入虚构样例（重复票 / 跨品类混放 / 部分召回 / 抽查冻结 /
票撤销 / 标签脱落 / 冷藏超温 / 即将到期票据），启动网页与进程内扫描。

参数：
    --host 127.0.0.1   --port 8080   --interval 30   --no-reset
"""
from __future__ import annotations

import argparse
import sys

from poultry import config
from poultry.database import init_db
from poultry.adapters import MockScale, MockTempProbe
from poultry.scanner import ScanWorker
from poultry.seed import build_fresh_service
from poultry.server import create_server
from poultry.services import MarketService


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="农贸市场禽肉检疫票与分割批次系统")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--interval", type=float, default=config.SCAN_INTERVAL,
                    help="票据到期/超温扫描间隔秒")
    ap.add_argument("--no-reset", action="store_true",
                    help="不重置数据库（默认重置并载入样例）")
    args = ap.parse_args(argv)

    if args.no_reset:
        init_db(config.DB_PATH)
        svc = MarketService(config.DB_PATH, scale=MockScale(), probe=MockTempProbe())
        print("沿用现有数据库", config.DB_PATH)
    else:
        print("重置数据库并载入虚构样例 ...")
        svc = build_fresh_service()
        for s in getattr(svc, "seed_summary", {}).get("scenarios", []):
            print("  -", s)
    print(f"数据目录：{config.DATA_DIR}（票据 {config.DOCS_DIR.name}/、"
          f"照片 {config.PHOTOS_DIR.name}/）")

    # 审计一次重量守恒，启动即暴露链路问题
    bad = [b for b in svc.conservation_audit() if not b["conserved"]]
    print(f"重量链路审计：{len(svc.conservation_audit())} 个放行批次，"
          f"守恒异常 {len(bad)} 个")

    server = create_server(args.host, args.port, svc=svc,
                           start_worker=False)
    worker = ScanWorker(svc, interval=args.interval,
                        logger=lambda m: print(m, flush=True))
    server.worker = worker
    worker.start()

    url = f"http://{args.host}:{args.port}/"
    print("=" * 64)
    print(f" 农贸市场禽肉检疫票与分割批次系统已启动：{url}")
    print(" 页面：到货验票 / 批次库存 / 冷藏格位 / 分割称重 / 托盘标签 /")
    print("       抽查冻结 / 召回清单 / 废弃物台账")
    print(" 角色：右上角切换 摊主(A01/A02/B01) 或 市场管理员")
    print(" 停止：Ctrl+C")
    print("=" * 64)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 ...")
        worker.stop()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
