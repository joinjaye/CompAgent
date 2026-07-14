"""时间格式转换工具：各源原始时间格式 → 统一的 UTC ISO8601（秒精度，Z 结尾）存库格式。

不同源的原始格式不同，见 CLAUDE.md【时间处理】。每加一个新格式的源，在这里加一个转换
函数，不要在各 collector 里各自算一遍 datetime。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def ms_to_iso(ms: Optional[int]) -> Optional[str]:
    """unix 毫秒 -> UTC ISO8601（Z 结尾，秒精度）。Zoomex / Lbank 用。"""
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_ms(iso: Optional[str]) -> Optional[int]:
    """UTC ISO8601（Z 结尾）-> unix 毫秒。ms_to_iso 的逆运算（精度只到秒，因为落库的
    ISO 字符串本来就丢弃了毫秒），用于跟原始毫秒时间戳做粗粒度比较。"""
    if iso is None:
        return None
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)
