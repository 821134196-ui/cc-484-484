"""全局配置与路径。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("POULTRY_DATA", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("POULTRY_DB", DATA_DIR / "market.db"))
DOCS_DIR = DATA_DIR / "docs"          # 虚构检疫票据（SVG）
PHOTOS_DIR = DATA_DIR / "photos"      # 现场照片（SVG）
STATIC_DIR = BASE_DIR / "poultry" / "web" / "static"

# 冷藏温度阈值（摄氏度）
COLD_TEMP_MIN = -4.0
COLD_TEMP_MAX = 4.0

# 检疫票默认有效期（天）—— 到货后仍需在有效期内
CERT_DEFAULT_VALID_DAYS = 3

# 允许的称重差异比例（实际到货重量 vs 票面重量）
WEIGHT_TOLERANCE = 0.02  # ±2%
# 分割净重相对毛重的出成率合理区间（分割 = 托盘 + 余料 + 废弃）
TRIM_YIELD_MIN = 0.85
TRIM_YIELD_MAX = 1.0001

# 废弃物异常阈值：单次废弃占来源重量超过该比例即预警
WASTE_RATIO_ALERT = 0.15

# 扫描间隔（秒）
SCAN_INTERVAL = 30


def ensure_dirs() -> None:
    for d in (DATA_DIR, DOCS_DIR, PHOTOS_DIR):
        d.mkdir(parents=True, exist_ok=True)
