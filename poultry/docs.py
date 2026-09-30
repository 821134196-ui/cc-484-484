"""本地虚构票据、托盘标签与现场照片（SVG 文本图，不使用肉类照片）。"""
from __future__ import annotations

from pathlib import Path

_W = 520
_BG = "#ffffff"
BLUE = "#1f6fb2"
GREEN = "#2e7d4f"
INK = "#1f2a33"
MUTED = "#5b6b78"
LINE = "#cdd9e2"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _bar(x: float, y: float, w: float, color: str) -> str:
    return f'<rect x="{x}" y="{y}" width="{w}" height="6" fill="{color}"/>'


def make_cert_svg(path: Path, *, cert_no: str, supplier: str, category: str,
                  weight: float, issued: str, valid_until: str) -> None:
    """检疫票缩略条（票据风格，蓝绿双色，非照片）。"""
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="150" viewBox="0 0 {_W} 150">
  <rect width="{_W}" height="150" fill="{_BG}" stroke="{LINE}"/>
  <rect x="0" y="0" width="8" height="150" fill="{GREEN}"/>
  <rect x="8" y="0" width="{_W-8}" height="26" fill="#eef5fb"/>
  <text x="22" y="18" font-family="sans-serif" font-size="13" font-weight="bold" fill="{BLUE}">动物检疫合格证明（虚构）</text>
  <text x="{_W-22}" y="18" text-anchor="end" font-family="monospace" font-size="12" fill="{GREEN}" font-weight="bold">票号 {cert_no}</text>
  <text x="22" y="52" font-family="sans-serif" font-size="12" fill="{MUTED}">供应商代号</text>
  <text x="110" y="52" font-family="sans-serif" font-size="13" fill="{INK}">{supplier}</text>
  <text x="260" y="52" font-family="sans-serif" font-size="12" fill="{MUTED}">品类</text>
  <text x="305" y="52" font-family="sans-serif" font-size="13" fill="{INK}">{category}</text>
  <text x="22" y="78" font-family="sans-serif" font-size="12" fill="{MUTED}">票面重量</text>
  <text x="110" y="78" font-family="sans-serif" font-size="13" font-weight="bold" fill="{INK}">{weight} kg</text>
  <text x="260" y="78" font-family="sans-serif" font-size="12" fill="{MUTED}">签发</text>
  <text x="305" y="78" font-family="sans-serif" font-size="11" fill="{INK}">{issued}</text>
  <text x="22" y="104" font-family="sans-serif" font-size="12" fill="{MUTED}">有效期至</text>
  <text x="110" y="104" font-family="monospace" font-size="12" fill="{BLUE}" font-weight="bold">{valid_until}</text>
  {_bar(22, 120, 90, BLUE)}{_bar(118, 120, 40, GREEN)}{_bar(164, 120, 60, "#9bbdd4")}{_bar(230, 120, 24, GREEN)}
  <text x="{_W-22}" y="128" text-anchor="end" font-family="sans-serif" font-size="10" fill="{MUTED}">本地虚构数据 · 仅用于系统演示</text>
</svg>"""
    _write(path, svg)


def make_label_svg(path: Path, *, tray_no: str, batch_no: str, category: str,
                   weight: float, stall: str, cut_no: str = "",
                   remark: str = "") -> None:
    """托盘标签（含批次链信息，便于扫码/核对）。"""
    rem_line = (f'<text x="22" y="118" font-family="sans-serif" font-size="11" '
                f'fill="{BLUE}">{remark}</text>' if remark else "")
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="140" viewBox="0 0 {_W} 140">
  <rect width="{_W}" height="140" fill="{_BG}" stroke="{LINE}"/>
  <rect x="0" y="0" width="{_W}" height="8" fill="{BLUE}"/>
  <text x="22" y="34" font-family="monospace" font-size="17" font-weight="bold" fill="{INK}">{tray_no}</text>
  <text x="{_W-22}" y="34" text-anchor="end" font-family="sans-serif" font-size="12" fill="{GREEN}" font-weight="bold">检疫合格 · 在架</text>
  <text x="22" y="60" font-family="sans-serif" font-size="12" fill="{MUTED}">品类</text>
  <text x="80" y="60" font-family="sans-serif" font-size="13" fill="{INK}">{category}</text>
  <text x="240" y="60" font-family="sans-serif" font-size="12" fill="{MUTED}">净重</text>
  <text x="285" y="60" font-family="sans-serif" font-size="13" font-weight="bold" fill="{INK}">{weight} kg</text>
  <text x="22" y="86" font-family="sans-serif" font-size="12" fill="{MUTED}">来源批次</text>
  <text x="110" y="86" font-family="monospace" font-size="12" fill="{BLUE}">{batch_no}</text>
  <text x="300" y="86" font-family="sans-serif" font-size="12" fill="{MUTED}">摊位</text>
  <text x="345" y="86" font-family="sans-serif" font-size="12" fill="{INK}">{stall}</text>
  <text x="22" y="112" font-family="sans-serif" font-size="11" fill="{MUTED}">分割单 {cut_no}</text>
  {rem_line}
  {_bar(22, 124, 40, BLUE)}{_bar(68, 124, 70, GREEN)}{_bar(144, 124, 30, "#9bbdd4")}
</svg>"""
    _write(path, svg)


def make_photo_svg(path: Path, *, title: str, lines: list[str]) -> None:
    """现场照片占位：几何图形 + 说明文字，明确非肉类装饰照片。"""
    body = "".join(
        f'<text x="24" y="{74 + i*22}" font-family="sans-serif" font-size="13" '
        f'fill="{INK}">{line}</text>'
        for i, line in enumerate(lines))
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="170" viewBox="0 0 {_W} 170">
  <rect width="{_W}" height="170" fill="#f4f8fb" stroke="{LINE}"/>
  <rect x="0" y="0" width="{_W}" height="30" fill="{BLUE}"/>
  <text x="16" y="20" font-family="sans-serif" font-size="13" font-weight="bold" fill="#fff">{title}</text>
  <circle cx="450" cy="95" r="46" fill="none" stroke="{GREEN}" stroke-width="3"/>
  <path d="M430 95 l14 14 l28 -30" fill="none" stroke="{GREEN}" stroke-width="4" stroke-linecap="round"/>
  {body}
  <text x="16" y="160" font-family="sans-serif" font-size="10" fill="{MUTED}">现场记录示意图（无肉类影像，纯信息图示）</text>
</svg>"""
    _write(path, svg)
